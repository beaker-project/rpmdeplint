
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

from __future__ import absolute_import

from collections import defaultdict
import binascii
import logging
import six
from six.moves import map
import hawkey
import rpm


logger = logging.getLogger(__name__)


class DependencySet(object):
    def __init__(self):
        self._packagedeps = defaultdict(lambda: dict(dependencies=[],problems=[]))
        self._packages_with_problems = set()
        self._overall_problems = set()
        self._repodeps = defaultdict(lambda: set())
        self._repo_packages = defaultdict(lambda: set())
        self._package_repo = {}

    def add_package(self, pkg, reponame, dependencies, problems):
        nevra = str(pkg)
        self._packagedeps[nevra]['dependencies'].extend(map(str, dependencies))
        self._repodeps[reponame].update([d.reponame for d in dependencies])
        self._repo_packages[reponame].add(nevra)
        self._package_repo[nevra] = reponame
        if len(problems) != 0:
            self._packagedeps[nevra]['problems'].extend(problems)
            self._packages_with_problems.add(nevra)
            self._overall_problems.update(problems)

    @property
    def packages(self):
        return list(self._packagedeps.keys())

    @property
    def overall_problems(self):
        return list(self._overall_problems)

    @property
    def packages_with_problems(self):
        return list(self._packages_with_problems)

    @property
    def package_dependencies(self):
        return dict(self._packagedeps)

    @property
    def repository_dependencies(self):
        return {key:list(value) for key, value in self._repodeps.items()}

    def repository_for_package(self, pkg):
        return self._package_repo[pkg]

    def dependencies_for_repository(self, reponame):
        return list(self._repodeps[reponame])

    def dependencies_for_package(self, nevra):
        return self._packagedeps[nevra]['dependencies']


class DependencyAnalyzer(object):
    """Context manager which checks packages against provided repos
    for dependency satisfiability.

    The analyzer will use a temporary directory to cache all downloaded
    repository data. The cache directory will be cleaned upon exit.
    """

    def __init__(self, repos, packages, sack=None):
        """
        :param repos: An iterable of rpmdeplint.repodata.Repo instances
        :param packages: An iterable of rpm package paths.
        """
        if sack is None:
            self._sack = hawkey.Sack(make_cache_dir=True)
        else:
            self._sack = sack

        self.repos_by_name = {}  #: mapping of (reponame, rpmdeplint.Repo)
        for repo in repos:
            repo.download_repodata()
            self._sack.load_yum_repo(repo=repo.as_hawkey_repo(), load_filelists=True)
            self.repos_by_name[repo.name] = repo

        self.packages = []  #: list of hawkeye.Package to be tested
        for rpmpath in packages:
            package = self._sack.add_cmdline_package(rpmpath)
            self.packages.append(package)

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        """Clean up cache directory to prevent from growing unboundedly."""
        for repo in self.repos_by_name.values():
            repo.cleanup_cache()

    def find_packages_that_require(self, name):
        pkgs = hawkey.Query(self._sack).filter(requires=name, latest_per_arch=True)
        return pkgs

    def find_packages_named(self, name):
        return hawkey.Query(self._sack).filter(name=name, latest_per_arch=True)

    def find_package_with_nevra(self, nevra):
        result = hawkey.Query(self._sack).filter(nevra=nevra)
        if result.count() == 0:
            return None
        else:
            return hawkey.Query(self._sack).filter(nevra=nevra)[0]

    def list_latest_packages(self):
        return hawkey.Query(self._sack).filter(latest_per_arch=True)

    def find_packages_for_repos(self, repos):
        pkgs = self.list_latest_packages()
        s = set(repos)
        return [x for x in pkgs if x.reponame in s]

    def download_package(self, package):
        if package in self.packages:
            # It's a package under test, nothing to download
            return package.location
        repo = self.repos_by_name[package.reponame]
        checksum_type = hawkey.chksum_name(package.chksum[0])
        checksum = binascii.hexlify(package.chksum[1]).decode('ascii')
        return repo.download_package(package.location, checksum_type=checksum_type, checksum=checksum)

    def try_to_install(self, *packages):
        """
        Try to solve the goal of installing the given package,
        starting from an empty package set.
        """
        g = hawkey.Goal(self._sack)
        for package in packages:
            g.install(package)
        results = dict(installs = [], upgrades = [], erasures = [], problems = [])
        install_succeeded = g.run()
        if install_succeeded:
            results['installs'] = g.list_installs()
            results['upgrades'] = g.list_upgrades()
            results['erasures'] = g.list_erasures()
        else:
            results['problems'] = g.problems

        return install_succeeded, results

    def try_to_install_all(self):
        ds = DependencySet()
        for pkg in self.packages:
            logger.debug('Solving install goal for %s', pkg)
            ok, results = self.try_to_install(pkg)
            ds.add_package(pkg, pkg.reponame, results['installs'], results['problems'])

        ok = len(ds.overall_problems) == 0
        return ok, ds

    def find_repoclosure_problems(self):
        problems = []
        available = hawkey.Query(self._sack).filter(latest_per_arch=True)
        available_from_repos = hawkey.Query(self._sack)\
                .filter(reponame__neq='@commandline').filter(latest_per_arch=True)
        # Filter out any obsoleted packages from the list of available packages.
        # It would be nice if latest_per_arch could do this for us, might make 
        # a good hawkey RFE...
        obsoleted = set()
        for pkg in available:
            if available.filter(obsoletes=[pkg]):
                logger.debug('Excluding obsoleted package %s', pkg)
                obsoleted.add(pkg)
        # XXX if pkg__neq were implemented we could just filter out obsoleted 
        # from the available query here
        obsoleted_from_repos = set()
        for pkg in available_from_repos:
            if available_from_repos.filter(obsoletes=[pkg]):
                logger.debug('Excluding obsoleted package %s from repos-only set', pkg)
                obsoleted_from_repos.add(pkg)
        for pkg in available:
            if pkg in self.packages:
                continue # checked by check-sat command instead
            if pkg in obsoleted:
                continue # no reason to check it
            logger.debug('Checking requires for %s', pkg)
            # XXX limit available packages to compatible arches?
            # (use libsolv archpolicies somehow)
            for req in pkg.requires:
                if six.text_type(req).startswith('rpmlib('):
                    continue
                providers = available.filter(provides=req)
                providers = [p for p in providers if p not in obsoleted]
                if not providers:
                    problem_msg = 'nothing provides {} needed by {}'.format(
                            six.text_type(req), six.text_type(pkg))
                    # If it's a pre-existing problem with repos (that is, the 
                    # problem also exists when the packages under test are 
                    # excluded) then warn about it here but don't consider it 
                    # a problem.
                    repo_providers = available_from_repos.filter(provides=req)
                    repo_providers = [p for p in repo_providers if p not in obsoleted_from_repos]
                    if not repo_providers:
                        logger.warn('Ignoring pre-existing repoclosure problem: %s', problem_msg)
                    else:
                        problems.append(problem_msg)
        return problems

    def _packages_have_explicit_conflict(self, left, right):
        """
        Returns True if the given packages have an explicit RPM-level Conflicts 
        declared between each other.
        """
        # XXX there must be a better way of testing for explicit Conflicts but 
        # the best I could find was to try solving the installation of both and 
        # checking the problem output...
        g = hawkey.Goal(self._sack)
        g.install(left)
        g.install(right)
        g.run()
        if g.problems and 'conflicts' in g.problems[0]:
            logger.debug('Found explicit Conflicts between %s and %s', left, right)
            return True
        return False

    def _file_conflict_is_permitted(self, left, right, filename):
        """
        Returns True if rpm would allow both the given packages to share 
        ownership of the given filename.
        """
        ts = rpm.TransactionSet()
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)

        left_hdr = ts.hdrFromFdno(open(left.location, 'rb'))
        right_hdr = ts.hdrFromFdno(open(self.download_package(right), 'rb'))
        left_files = rpm.files(left_hdr)
        right_files = rpm.files(right_hdr)
        if left_files[filename].matches(right_files[filename]):
            logger.debug('Conflict on %s between %s and %s permitted because files match',
                    filename, left, right)
            return True
        if left_files[filename].color != right_files[filename].color:
            logger.debug('Conflict on %s between %s and %s permitted because colors differ',
                    filename, left, right)
            return True
        return False

    def find_conflicts(self):
        """
        Find undeclared file conflicts in the packages under test.
        Returns a list of strings describing each conflict found
        (or empty list if no conflicts were found).
        """
        problems = []
        for package in self.packages:
            for filename in package.files:
                for conflicting in hawkey.Query(self._sack).filter(file=filename, latest_per_arch=True):
                    if conflicting == package:
                        continue
                    if self._packages_have_explicit_conflict(package, conflicting):
                        continue
                    logger.debug('Considering conflict on %s with %s', filename, conflicting)
                    if not self._file_conflict_is_permitted(package, conflicting, filename):
                        problems.append(u'{} provides {} which is also provided by {}'.format(
                                six.text_type(package), filename, six.text_type(conflicting)))
        return problems

    def find_upgrade_problems(self):
        """
        Checks for any package in the repos which would upgrade or obsolete the 
        packages under test.
        Returns a list of strings describing each upgrade problem found (or 
        empty list if no problems were found).
        """
        problems = []
        for package in self.packages:
            for newer in hawkey.Query(self._sack).filter(name=package.name, arch=package.arch, evr__gt=package.evr):
                problems.append(u'{} would be upgraded by {} from repo {}'.format(
                        six.text_type(package), six.text_type(newer), newer.reponame))
            for obsoleting in hawkey.Query(self._sack).filter(obsoletes=[package]):
                problems.append(u'{} would be obsoleted by {} from repo {}'.format(
                        six.text_type(package), six.text_type(obsoleting), obsoleting.reponame))
        return problems