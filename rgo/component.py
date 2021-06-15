# -*- coding: utf-8 -*-
#
# Copyright © 2016 Igor Gnatenko <ignatenko@redhat.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.


import os
import re
import shutil
import subprocess

import rpm

from . import LOGGER, utils
from .git import PatchesAction


SRPM_RE = re.compile(r".+\.(?:no)?src\.rpm")

class Component(object):
    def __init__(self, name, version_from=None, git=None, distgit=None, requires=[]):
        """
        :param str name: Name of component
        :param rgo.git.Git: Git repository
        :param rgo.git.DistGit: Dist-Git repository
        """
        assert git or distgit
        self.name = name

        self.git = git
        self.distgit = distgit
        self.version_from = version_from

        if self.version_from is None:
            if self.distgit:
                self.version_from = "git"
            else:
                self.version_from = "spec"

        self.requires = set(requires)
        self.cloned = False

        self.build_id = None
        self.done = None
        self.srpm = None
        self.state = None
        self.success = None

    def __repr__(self): # pragma: no cover
        return "<Component {0.name!r}: git({0.git!r}) distgit({0.distgit})>".format(self)

    def clone(self, dest):
        """
        :param str dest: Directory for git repos to clone in
        """
        if self.git:
            self.git.clone(os.path.join(dest, self.name))
        if self.distgit:
            self.distgit.clone(os.path.join(dest, "{!s}-distgit".format(self.name)))
        self.cloned = True

    def make_srpm(self, tmpdir):
        """
        Build SRPM.

        :param str tmpdir: Temporary directory to work in
        """
        workdir = os.path.join(tmpdir, self.name)
        os.mkdir(workdir)

        assert self.cloned
        LOGGER.info("Building (no)source RPM for component {}".format(self.name))
        if self.distgit:
            spec_git = self.distgit
            spec_name = "{!s}.spec".format(self.name)
            patches = self.distgit.patches
        else:
            spec_git = self.git
            spec_name = self.git.spec_path or "{!s}.spec".format(self.name)
            patches = PatchesAction.keep

        # extract spec file from specified branch and save it in temporary location
        spec = os.path.join(workdir, "original.spec")
        with open(spec, "w") as f_spec:
            subprocess.run(["git", "cat-file", "-p", "{}:{}".format(spec_git.ref, spec_name)],
                           cwd=spec_git.cwd, check=True, stdout=f_spec)

        rpmspec = rpm.spec(spec)

        spec_name = rpmspec.sourceHeader["Name"]
        if isinstance(spec_name, bytes):
            spec_name = spec_name.decode("utf-8")

        spec_version = rpmspec.sourceHeader["Version"]
        if isinstance(spec_version, bytes):
            spec_version = spec_version.decode("utf-8")

        _spec_path = os.path.join(workdir, "{!s}.spec".format(spec_name))

        if self.git:
            version, release = self.git.describe(self.name, spec_version=spec_version, version_from=self.version_from)

            # Prepare archive
            if release == "1":
                # tagged version, no need to add useless numbers
                nvr = "{!s}-{!s}".format(self.name, version)
            else:
                nvr = "{!s}-{!s}-{!s}".format(self.name, version, release)
            archive = os.path.join(workdir, "{!s}.tar.xz".format(nvr))
            LOGGER.debug("Preparing archive %r", archive)
            proc = subprocess.run(["git", "archive", "--prefix={!s}/".format(nvr),
                                   "--format=tar", self.git.ref],
                                  cwd=self.git.cwd, check=True,
                                  stdout=subprocess.PIPE)
            with open(archive, "w") as f_archive:
                subprocess.run(["xz", "-z", "--threads=0", "-"],
                               check=True, input=proc.stdout, stdout=f_archive)

            # Prepare new spec
            with open(_spec_path, "w") as specfile:
                prepared = "{!s}\n{!s}".format(
                    utils.prepare_spec(spec, version, release, nvr, patches),
                    utils.generate_changelog(self.git.timestamp, version, release, self.git.get_rpm_changelog()))
                specfile.write(prepared)
        else:
            # Just copy spec from distgit
            shutil.copy2(spec, _spec_path)
        spec = _spec_path

        _sources = []
        for src, _, src_type in rpm.spec(spec).sources:
            if src_type == rpm.RPMBUILD_ISPATCH:
                if patches == PatchesAction.keep:
                    _sources.append(src)
            elif src_type == rpm.RPMBUILD_ISSOURCE:
                # src in fact is url, but we need filename. We don't want to get
                # Content-Disposition from HTTP headers, because link could be not
                # available anymore or broken.
                src = os.path.basename(src)
                if self.git and src == os.path.basename(archive):
                    # Skip sources which are just built
                    continue
                _sources.append(src)
        if _sources and not self.distgit:
            raise NotImplementedError("Patches/Sources are applied in upstream")
        # Copy sources/patches from distgit
        for source in _sources:
            shutil.copy2(os.path.join(self.distgit.cwd, source),
                         os.path.join(workdir, source))

        # Build .(no)src.rpm
        try:
            result = subprocess.run(["rpmbuild", "-bs", _spec_path, "--nodeps",
                                     "--define", "_topdir {!s}".format(workdir),
                                     "--define", "_sourcedir {!s}".format(workdir)],
                                    check=True, universal_newlines=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as err:
            LOGGER.critical("Failed to build (no)source RPM:\n%s", err.output)
            raise
        else:
            LOGGER.debug(result.stdout)
        srpms_dir = os.path.join(workdir, "SRPMS")
        srpms = [f for f in os.listdir(srpms_dir) if SRPM_RE.search(f)]
        assert len(srpms) == 1, "We expect 1 .(no)src.rpm, but we found: {!r}".format(srpms)

        self.srpm = os.path.join(tmpdir, srpms[0])
        shutil.move(os.path.join(srpms_dir, srpms[0]), self.srpm)
        shutil.rmtree(workdir)

        LOGGER.info("Built (no)source RPM: %r from %r", self.srpm, self.git.ref_info())
        return self.srpm
