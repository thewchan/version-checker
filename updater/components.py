from loguru import logger
from packaging.version import parse
import requests
from cachier import cachier
import datetime
import yaml
from pathlib import Path
from rex import rex
from subprocess import run
from abc import ABC, abstractmethod
from plumbum import local
from updater import git_check, plumbum_msg
import click
import copy
import maya


class Config:

    STATE_FILES_UPDATED = "STATE_FILES_UPDATED"
    STATE_TEST_RUN = "STATE_TEST_RUN"
    STATE_CONFIG_SAVED = "STATE_CONFIG_SAVED"
    STATE_COMMITED_CHANGES = "STATE_COMMITED_CHANGES"

    def __init__(self, components_yaml_file=None):
        self.components = []
        self.config_file = components_yaml_file
        if self.config_file and not self.config_file.is_file():
            logger.info("Config file %r does not exists." % str(self.config_file))
        self.test_command = None
        self.test_dir = None
        self.git_commit = True
        self.status = {}

    def update_status(self, component, step):
        if component.component_name not in self.status:
            self.status["component.component_name"] = {}
        comp = self.status["component.component_name"]
        comp[step] = str(maya.now())

    def add(self, component):
        self.components.append(component)
        return self.components.index(self.components[-1])

    def components_to_dict(self):
        return {
            component.component_name: component.to_dict()
            for component in self.components
        }

    def save_to_yaml(self, file=None):
        file_to_save = Path(file) if file is not None else self.config_file
        yaml.dump(self.components_to_dict(), open(file_to_save, "w"))

    def save_config(self, destination_file=None, dry_run=False, print_yaml=False):
        if not dry_run:
            if destination_file:
                self.save_to_yaml(destination_file)
            elif self.config_file:
                self.save_to_yaml()

        if print_yaml:
            print(yaml.dump(self.components_to_dict()))

    def read_from_yaml(self, file=None, clear_components=True):
        read_file = file or self.config_file
        if read_file and read_file.is_file():
            components_dict = yaml.safe_load(open(read_file))
        else:
            components_dict = {}

        if clear_components:
            self.components = []

        for component_name in components_dict:
            comp = components_dict[component_name]
            params = {
                "component_type": comp["component-type"],
                "component_name": component_name,
                "current_version_tag": comp["current-version"],
                "repo_name": comp.get("docker-repo", Component.REPO_DEFAULT),
            }
            last_index = self.add(factory.get(**params))
            self.components[last_index].repo_name = comp.get(
                "docker-repo", Component.REPO_DEFAULT
            )
            self.components[last_index].prefix = comp.get(
                "prefix", Component.PREFIX_DEFAULT
            )
            self.components[last_index].filter = comp.get(
                "filter", Component.FILTER_DEFAULT
            )
            self.components[last_index].files = comp.get(
                "files", Component.FILES_DEFAULT
            )
            self.components[last_index].exclude_versions = comp.get(
                "exclude-versions", Component.EXLUDE_VERSIONS_DEFAULT
            )

    def count_components_to_update(self):
        self.check()
        return sum(
            [1 for component in self.components if component.newer_version_exists()]
        )

    def check(self):
        return [(comp.component_name, comp.check()) for comp in self.components]

    def run_tests(self, processed_component):
        ret = run(self.test_command, cwd=(self.test_dir or self.config_file.parent))
        assert ret.returncode == 0, (
            click.style("Error!", fg="red")
            + "( "
            + processed_component.component_name
            + " ) "
            + str(ret)
        )

    def commit_changes(self, component, dry_run):
        git = local["git"]
        with local.cwd(self.config_file.parent):
            ret = git_check(git["diff", "--name-only"].run(retcode=None))
            changed_files = ret[1].splitlines()
            assert set(component.files).issubset(
                set(changed_files)
            ), "Not all SRC files are in git changed files.\n" + plumbum_msg(ret)
            if not dry_run:
                git_check(git["add", self.config_file.name].run(retcode=None))
                for file_name in component.files:
                    git_check(git["add", file_name].run(retcode=None))
                git_check(
                    git["commit", "--message=%s" % component.component_name].run(
                        retcode=None
                    )
                )

    def update_files(self, base_dir, dry_run=False):
        counter = 0
        for component in self.components:
            if component.newer_version_exists():
                counter += component.update_files(base_dir, dry_run)
            self.update_status(component, self.STATE_FILES_UPDATED)
            if self.test_command:
                self.run_tests(component)
                self.update_status(component, self.STATE_TEST_RUN)

            if not dry_run:
                component.current_version = copy.deepcopy(component.next_version)
                component.current_version_tag = copy.deepcopy(
                    component.next_version_tag
                )
            self.save_config(dry_run=dry_run)
            self.update_status(component, self.STATE_CONFIG_SAVED)

            if self.git_commit:
                self.commit_changes(component, dry_run)
                self.update_status(component, self.STATE_COMMITED_CHANGES)

        return counter

    def get_versions_info(self):
        new = [
            c.component_name
            + " - current: "
            + c.current_version_tag
            + " next: "
            + (
                click.style(c.next_version_tag, fg="green")
                if c.newer_version_exists()
                else click.style(c.next_version_tag, fg="yellow")
            )
            for c in self.components
        ]
        new.sort()
        return new


class Component(ABC):

    PREFIX_DEFAULT = None
    FILTER_DEFAULT = "/.*/"
    FILES_DEFAULT = None
    EXLUDE_VERSIONS_DEFAULT = []
    REPO_DEFAULT = None
    LATEST_TAGS = ["latest"]

    def __init__(self, component_name, current_version_tag):
        self.component_type = None
        self.component_name = component_name
        self.current_version_tag = current_version_tag
        self.current_version = parse(current_version_tag)
        self.version_tags = []
        self.next_version = self.current_version
        self.next_version_tag = self.current_version_tag
        self.prefix = self.PREFIX_DEFAULT
        self.filter = self.FILTER_DEFAULT
        self.files = self.FILES_DEFAULT
        self.exclude_versions = self.EXLUDE_VERSIONS_DEFAULT
        super().__init__()

    def newer_version_exists(self):
        if self.current_version_tag in self.LATEST_TAGS:
            return False
        else:
            return self.next_version > self.current_version

    @abstractmethod
    def fetch_versions():
        pass

    def check(self):
        if self.current_version_tag not in self.LATEST_TAGS:
            self.version_tags = self.fetch_versions()

            self.next_version = max(
                [
                    parse(tag)
                    for tag in self.version_tags
                    if (tag == rex(self.filter)) and tag not in self.exclude_versions
                ]
            )
            self.next_version_tag = (self.prefix or "") + str(self.next_version)

        return self.newer_version_exists()

    def to_dict(self):
        ret = {
            "component-type": self.component_type,
            "current-version": self.current_version_tag,
            "next-version": self.next_version_tag,
        }
        # if self.current_version_tag != self.next_version_tag:
        #     ret["next-version"] = self.next_version_tag
        if self.prefix != self.PREFIX_DEFAULT:
            ret["prefix"] = self.prefix
        if self.filter != self.FILTER_DEFAULT:
            ret["filter"] = self.filter
        if self.files != self.FILES_DEFAULT:
            ret["files"] = self.files
        if self.exclude_versions != self.EXLUDE_VERSIONS_DEFAULT:
            ret["exclude-versions"] = self.exclude_versions
        return ret

    def name_version_tag(self, version_tag):
        return version_tag

    def count_occurence(self, string_to_search):
        return string_to_search.count(self.name_version_tag(self.current_version_tag))

    def replace(self, string_to_replace):
        return string_to_replace.replace(
            self.name_version_tag(self.current_version_tag),
            self.name_version_tag(self.next_version_tag),
        )

    def update_files(self, base_dir, dry_run=False):
        counter = 0

        for file_name in self.files:
            file = Path(Path(base_dir) / file_name)
            orig_content = file.read_text()
            assert self.count_occurence(orig_content) <= 1, (
                "To many verison of %s occurence in %s!"
                % (self.current_version_tag, orig_content)
            )
            if not dry_run:
                new_content = self.replace(orig_content)
                assert new_content != orig_content, (
                    "Error in version replacment for %s: no replacement done for current_version"
                    % self.component_name
                    + ": %s and next_version: %s\nOrigin\n%s\nNew\n%s."
                    % (
                        self.current_version_tag,
                        self.next_version_tag,
                        orig_content,
                        new_content,
                    )
                )
                file.write_text(new_content)
            counter += 1
        return counter


@cachier(stale_after=datetime.timedelta(days=3))
def fetch_docker_images_versions(repo_name, component_name):
    logger.info(repo_name + ":" + component_name + " - NOT CACHED")
    payload = {
        "service": "registry.docker.io",
        "scope": "repository:{repo}/{image}:pull".format(
            repo=repo_name, image=component_name
        ),
    }

    r = requests.get("https://auth.docker.io/token", params=payload)
    if not r.status_code == 200:
        print("Error status {}".format(r.status_code))
        raise Exception("Could not get auth token")

    j = r.json()
    token = j["token"]
    h = {"Authorization": "Bearer {}".format(token)}
    r = requests.get(
        "https://index.docker.io/v2/{}/{}/tags/list".format(repo_name, component_name),
        headers=h,
    )
    return r.json().get("tags", [])


@cachier(stale_after=datetime.timedelta(days=3))
def fetch_pypi_versions(component_name):
    r = requests.get("https://pypi.org/pypi/{}/json".format(component_name))
    # it returns 404 if there is no such a package
    if not r.status_code == 200:
        return list()
    else:
        return list(r.json().get("releases", {}).keys())


class DockerImageComponent(Component):
    """docstring for ClassName"""

    def __init__(self, repo_name, component_name, current_version_tag):
        super(DockerImageComponent, self).__init__(component_name, current_version_tag)
        self.repo_name = repo_name
        self.component_type = "docker-image"

    def fetch_versions(self):
        return fetch_docker_images_versions(self.repo_name, self.component_name)

    def to_dict(self):
        ret = super(DockerImageComponent, self).to_dict()
        ret["docker-repo"] = self.repo_name
        return ret

    def name_version_tag(self, version_tag):
        return self.component_name + ":" + version_tag


class PypiComponent(Component):
    """docstring for ClassName"""

    def __init__(self, component_name, current_version_tag, **_ignored):
        super(PypiComponent, self).__init__(component_name, current_version_tag)
        self.component_type = "pypi"

    def fetch_versions(self):
        return fetch_pypi_versions(self.component_name)

    def name_version_tag(self, version_tag):
        return self.component_name + "==" + version_tag


class ComponentFactory:
    def get(self, component_type, **args):
        if component_type == "docker-image":
            return DockerImageComponent(**args)
        elif component_type == "pypi":
            return PypiComponent(**args)
        else:
            raise ValueError(component_type)


factory = ComponentFactory()
