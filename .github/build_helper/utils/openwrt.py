# SPDX-FileCopyrightText: Copyright (c) 2024 沉默の金 <cmzj@cmzj.org>
# SPDX-License-Identifier: MIT
import os
import re
import subprocess
from typing import Literal

import pygit2
from actions_toolkit import core

from .logger import logger
from .network import request_get
from .utils import apply_patch


def create_patch_from_unstaged(repo_path: str) -> str | None:
    # Open the repository
    repo = pygit2.Repository(repo_path)

    orig = repo.head.target

    # Check for unstaged changes
    index = repo.index
    unstaged_changes = index.diff_to_workdir()

    if not unstaged_changes.deltas:
        logger.debug("%s 没有未提交的更改", repo_path)
        return None

    # Create a new temporary commit for unstaged changes
    temp_commit_message = "Temporary commit for patch generation"
    author = repo.default_signature
    index.add_all()  # Add all unstaged files
    tree = index.write_tree()
    temp_commit_oid = repo.create_commit(
        'HEAD', author, author, temp_commit_message, tree, [repo.head.target],
    )

    # Generate a diff between the temporary commit and the previous commit
    diff = repo.diff(orig, temp_commit_oid)

    # Reset the repository to the previous commit to undo the temporary commit
    repo.reset(orig, pygit2.enums.ResetMode.MIXED)

    return diff.patch

class OpenWrt:
    def __init__(self, path: str, tag_branch: str) -> None:
        self.path = path
        self.repo = pygit2.Repository(self.path)
        self.set_tag_or_branch(tag_branch)

    def set_tag_or_branch(self, tag_branch: str) -> None:
        if tag_branch in self.repo.branches:
            # 分支
            self.repo.checkout(tag_branch)
        else:
            # 标签
            tag_ref = self.repo.lookup_reference(f"refs/tags/{tag_branch}")
            treeish = self.repo.get(tag_ref.target)
            if treeish:
                self.repo.checkout_tree(treeish)
                self.repo.head.set_target(tag_ref.target)
            else:
                msg = f"标签{tag_branch}不存在"
                raise ValueError(msg)

        self.tag_branch = tag_branch

    def feed_update(self) -> None:
        subprocess.run([os.path.join(self.path, "scripts", "feeds"), 'update', '-a'], cwd=self.path)

    def feed_install(self) -> None:
        subprocess.run([os.path.join(self.path, "scripts", "feeds"), 'install', '-a'], cwd=self.path)

    def make_defconfig(self) -> None:
        subprocess.run(['make', 'defconfig'], cwd=self.path)

    def apply_config(self, config: str) -> None:
        with open(os.path.join(self.path, '.config'), 'w') as f:
            f.write(config)

    def get_diff_config(self) -> str:
        return subprocess.run([os.path.join(self.path, "scripts", "diffconfig.sh")], cwd=self.path, capture_output=True, text=True).stdout

    def get_kernel_version(self) -> str | None:
        kernel_version = None
        if os.path.isfile(os.path.join(self.path, '.config')):
            with open(os.path.join(self.path, '.config')) as f:
                for line in f:
                    if line.startswith('CONFIG_LINUX_'):
                        match = re.match(r'^CONFIG_LINUX_(?P<major>[0-9]+)_(?P<minor>[0-9]+)=y$', line)
                        if match:
                            kernel_version = f"{match.group("major")}.{match.group("minor")}"
                            break
        logger.debug("仓库%s的内核版本为%s", self.path, kernel_version)
        return kernel_version

    def get_arch(self) -> tuple[str | None, str | None]:
        arch = None
        version = None
        if os.path.isfile(os.path.join(self.path, '.config')):
            with open(os.path.join(self.path, '.config')) as f:
                for line in f:
                    if line.startswith('CONFIG_ARCH='):
                        match = re.match(r'^CONFIG_ARCH="(?P<arch>.*)"$', line)
                        if match:
                            arch = match.group("arch")
                            break
                    elif line.startswith('CONFIG_arm_'):
                        match = re.match(r'^CONFIG_arm_(?P<ver>[0-9]+)=y$', line)
                        if match:
                            version = match.group("ver")
                            break
        logger.debug("仓库%s的架构为%s,版本为%s", self.path, arch, version)
        return arch, version

    def get_package_config(self, package: str) -> Literal["y", "n", "m"] | None:
        package_config = None
        if os.path.isfile(os.path.join(self.path, '.config')):
            with open(os.path.join(self.path, '.config')) as f:
                for line in f:
                    if line.startswith(f'CONFIG_PACKAGE_{package}='):
                        match = re.match(fr'^CONFIG_PACKAGE_{package}=(?P<config>[ymn])$', line)
                        if match:
                            package_config = match.group("config")
                            if  package_config in ("y", "n", "m"):
                                break
                            package_config = None
        else:
            logger.warning("仓库%s的配置文件不存在", self.path)
        logger.debug("仓库%s的软件包%s的配置为%s", self.path, package, package_config)
        return package_config # type: ignore[]

    def check_package_dependencies(self) -> bool:
        subprocess.run(['gmake', '-s', 'prepare-tmpinfo'], cwd=self.path)
        if err := subprocess.run(['./scripts/package-metadata.pl', 'mk', 'tmp/.packageinfo'], cwd=self.path, capture_output=True, text=True).stderr:
            core.error(f'检查到软件包依赖问题,这有可能会导致编译错误:\n{err}')
            return False
        return True

    def fix_problems(self) -> None:
        if self.tag_branch not in ("main", "master"):
            #https://github.com/openwrt/openwrt/commit/ecc53240945c95bc77663b79ccae6e2bd046c9c8
            patch = request_get("https://github.com/openwrt/openwrt/commit/ecc53240945c95bc77663b79ccae6e2bd046c9c8.patch")
            if patch:
                if not apply_patch(patch, self.path):
                    core.error("修复内核模块依赖失败, 这可能会导致编译错误。\nhttps://github.com/openwrt/openwrt/commit/ecc53240945c95bc77663b79ccae6e2bd046c9c8")
            else:
                core.error("获取内核模块依赖修复补丁失败, 这可能会导致编译错误。\nhttps://github.com/openwrt/openwrt/commit/ecc53240945c95bc77663b79ccae6e2bd046c9c8")

        # 替换dnsmasq为dnsmasq-full
        logger.info("替换dnsmasq为dnsmasq-full")
        with open(os.path.join(self.path, 'include', 'target.mk'), encoding='utf-8') as f:
            content = re.sub(r"^	dnsmasq \\", r"	dnsmasq \\", f.read())
        with open(os.path.join(self.path, 'include', 'target.mk'), 'w', encoding='utf-8') as f:
            f.write(content)
        # 修复broadcom.mk中的路径错误
        logger.info("修复broadcom.mk中的路径错误")
        with open(os.path.join(self.path, 'package', "kernel", "mac80211", "broadcom.mk"), encoding='utf-8') as f:
            content = re.sub(r'	b43-fwsquash.py "$(CONFIG_B43_FW_SQUASH_PHYTYPES)" "$(CONFIG_B43_FW_SQUASH_COREREVS)"',
                             r'	$(TOPDIR)/tools/b43-tools/files/b43-fwsquash.py "$(CONFIG_B43_FW_SQUASH_PHYTYPES)" "$(CONFIG_B43_FW_SQUASH_COREREVS)"',
                             f.read())
        with open(os.path.join(self.path, 'package', "kernel", "mac80211", "broadcom.mk"), 'w', encoding='utf-8') as f:
            f.write(content)

        if self.tag_branch == "v23.05.2":
            logger.info("修复iperf3冲突")
            patch = request_get("https://github.com/openwrt/packages/commit/cea45c75c0153a190ee41dedaf6526ae08e33928.patch")
            if patch:
                if not apply_patch(patch, os.path.join(self.path, "feeds", "packages")):
                    core.error("修复iperf3冲突失败, 这可能会导致编译错误。\nhttps://github.com/openwrt/packages/commit/cea45c75c0153a190ee41dedaf6526ae08e33928")
            else:
                core.error("获取iperf3冲突修复补丁失败, 这可能会导致编译错误。\nhttps://github.com/openwrt/packages/commit/cea45c75c0153a190ee41dedaf6526ae08e33928")

        if self.tag_branch == "v23.05.3":
            logger.info("修复libpfring")
            patch1 = request_get("https://github.com/openwrt/packages/commit/534bd518f3fff6c31656a1edcd7e10922f3e06e5.patch")
            patch2 = request_get("https://github.com/openwrt/packages/commit/c3a50a9fac8f9d8665f8b012abd85bb9e461e865.patch")
            if patch1 and patch2:
                if not (apply_patch(patch1, os.path.join(self.path, "feeds", "packages")) and
                        apply_patch(patch2, os.path.join(self.path, "feeds", "packages"))):
                    core.error("修复libpfring失败, 这可能会导致编译错误。\nttps://github.com/openwrt/packages/commit/c3a50a9fac8f9d8665f8b012abd85bb9e461e865")
            else:
                core.error("获取libpfring修复补丁失败, 这可能会导致编译错误。\nttps://github.com/openwrt/packages/commit/c3a50a9fac8f9d8665f8b012abd85bb9e461e865")



    def get_pacthes(self) -> dict:
        result = {"openwrt": create_patch_from_unstaged(self.path),
                  "feeds": {}}
        for feed in os.listdir(os.path.join(self.path, "feeds")):
            path = os.path.join(self.path, "feeds", feed)
            if os.path.isdir(path) and os.path.isdir(os.path.join(path, ".git")) and (patch := create_patch_from_unstaged(path)):
                result["feeds"][feed] = patch
        return result
