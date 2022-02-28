#!/usr/bin/env python3
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
Generate pull requests that update a repository's Go version.

Classes:

    GoPRMaker

"""
import json
import os
import re
import subprocess
import sys
from typing import Optional, TypedDict, Any, Final

import requests

from github.Branch import Branch
from github.Commit import Commit
from github.ContentFile import ContentFile
from github.GitCommit import GitCommit
from github.GithubException import BadCredentialsException, GithubException, UnknownObjectException
from github.GithubObject import NotSet
from github.GitRef import GitRef
from github.GitTree import GitTree
from github.InputGitAuthor import InputGitAuthor
from github.InputGitTreeElement import InputGitTreeElement
from github.Label import Label
from github.MainClass import Github
from github.PullRequest import PullRequest
from github.Repository import Repository
from github.Requester import Requester

from pr_to_update_go.constants import ENV_GITHUB_TOKEN, GO_VERSION_URL, ENV_GITHUB_REPOSITORY, \
	ENV_GITHUB_REPOSITORY_OWNER, GO_REPO_NAME, RELEASE_PAGE_URL, ENV_GO_VERSION_FILE, \
	ENV_GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL_TEMPLATE

class GoVersion(TypedDict):
	"""
	A single entry in the list returned by the Go website's version listing API.
	"""
	#: The type of files is unimportant, because it's not used
	files: list[Any]
	stable: bool
	version: str

def get_pr_body(go_version: str, milestone_url: str) -> str:
	"""
	Generates the body of a Pull Request given a Go release version and a
	URL that points to information about what changes were in said release.
	"""
	with open(os.path.join(os.path.dirname(__file__), '/pr_template.md'), encoding="UTF-8") as file:
		pr_template = file.read()
	go_major_version = get_major_version(go_version)

	release_notes = get_release_notes(go_version)
	pr_body = pr_template.format(GO_VERSION=go_version, GO_MAJOR_VERSION=go_major_version,
		RELEASE_NOTES=release_notes, MILESTONE_URL=milestone_url)
	print('Templated PR body')
	return pr_body

def get_major_version(from_go_version: str) -> str:
	"""
	Extracts the "major" version part of a full Go release version. ("major" to
	the Go project is the part of the version that most people think of as the
	major and minor versions - refer to examples).

	>>> get_major_version("1.23.45-6rc7")
	'1.23'
	>>> get_major_version("not a release version")
	''
	"""
	match = re.search(pattern=r'^\d+\.\d+', string=from_go_version)
	if match:
		return match.group(0)
	return ""

def getenv(var: str) -> str:
	"""
	Returns the value of the environment variable with the given name.

	If ``var`` is not set in the execution environment, a KeyError is raised.

	>>> os.environ["FOO"] = "BAR"
	>>> getenv("FOO")
	'BAR'
	"""
	return os.environ[var]

get_repo_name = lambda: getenv(ENV_GITHUB_REPOSITORY)
get_repo_owner = lambda: getenv(ENV_GITHUB_REPOSITORY_OWNER)

def get_release_notes(go_version: str) -> str:
	"""
	Gets the release notes for the given Go version.
	"""
	release_history_response = requests.get(RELEASE_PAGE_URL)
	release_history_response.raise_for_status()
	release_notes_content = release_history_response.content.decode()
	go_version_pattern = go_version.replace('.', '\\.')
	release_notes_pattern: str = f'<p>\\s*\\n\\s*go{go_version_pattern}.*?</p>'
	release_notes_matches = re.search(release_notes_pattern, release_notes_content,
		re.MULTILINE | re.DOTALL)
	if release_notes_matches is None:
		raise Exception(f'Could not find release notes on {RELEASE_PAGE_URL}')
	release_notes = re.sub(r'[\s\t]+', ' ', release_notes_matches.group(0))
	return release_notes


def get_latest_major_upgrade(from_go_version: str) -> str:
	"""
	Gets the version of the latest Go release that is the same "major"
	version as the passed current (or "from") Go version.

	If no stable version is found that is the same "major" version as the
	given current version, an exception is raised.
	"""
	major_version = get_major_version(from_go_version)
	go_version_response = requests.get(GO_VERSION_URL)
	go_version_response.raise_for_status()
	go_version_content: list[GoVersion] = json.loads(go_version_response.content)
	fetched_go_version: str = ''
	for go_version in go_version_content:
		if not go_version["stable"]:
			continue
		match = re.search(r"[\d.]+", go_version["version"])
		if not match:
			continue
		fetched_go_version = match.group(0)
		if major_version == get_major_version(fetched_go_version):
			break
	else:
		raise Exception(f'No supported {major_version} Go versions exist.')
	print(f'Latest version of Go {major_version} is {fetched_go_version}')
	return fetched_go_version


class GoPRMaker:
	"""
	A class to generate pull requests for the purpose of updating the Go version
	in a repository.
	"""
	gh_api: Github
	latest_go_version: str
	repo: Repository
	author: InputGitAuthor

	def __init__(self, gh_api: Github):
		self.gh_api = gh_api
		self.repo = self.get_repo(get_repo_name())

		try:
			git_author_name = getenv(ENV_GIT_AUTHOR_NAME)
			git_author_email = GIT_AUTHOR_EMAIL_TEMPLATE.format(git_author_name=git_author_name)
			self.author = InputGitAuthor(git_author_name, git_author_email)
		except KeyError:
			self.author = NotSet
			print('Will commit using the default author')

	def branch_exists(self, branch: str) -> bool:
		"""
		Checks the existence of a given branch in the repository.
		"""
		try:
			repo_go_version = self.get_repo_go_version(branch)
			if self.latest_go_version == repo_go_version:
				print(f'Branch {branch} already exists')
				return True
		except GithubException as e:
			message = e.data["message"]
			if not isinstance(message, str) or not re.match("No commit found for the ref", message):
				raise e
		return False

	def update_branch(self, branch_name: str, sha: str) -> None:
		"""
		Updates the branch given by ``branch_name`` on the remote origin by
		fast-forwarding it to a commit given by its hash in ``sha``.

		Note that only fast-forward updates are possible, as this doesn't
		"force" push.
		"""
		requester: Requester = self.repo._requester
		patch_parameters = {
			'sha': sha,
		}
		requester.requestJsonAndCheck(
			'PATCH', self.repo.url + f'/git/refs/heads/{branch_name}', input=patch_parameters
		)

	def run(self, update_version_only: bool = False) -> None:
		"""
		This is the 'main' method of the PR maker, which does everything
		necessary to create the PR that will update the repository's Go version.
		"""
		repo_go_version = self.get_repo_go_version()
		self.latest_go_version = get_latest_major_upgrade(repo_go_version)
		commit_message: str = f'Update Go version to {self.latest_go_version}'

		source_branch_name: str = f'go-{self.latest_go_version}'
		target_branch: str = 'master'
		if repo_go_version == self.latest_go_version:
			print(f'Go version is up-to-date on {target_branch}, nothing to do.')
			return

		commit: Optional[Commit] = None
		if not self.branch_exists(source_branch_name):
			commit = self.set_go_version(self.latest_go_version, commit_message,
				source_branch_name)
		if commit is None:
			source_branch_ref: GitRef = self.repo.get_git_ref(f'heads/{source_branch_name}')
			commit = self.repo.get_commit(source_branch_ref.object.sha)
		subprocess.run(['git', 'fetch', 'origin'], check=True)
		subprocess.run(['git', 'checkout', commit.sha], check=True)
		if update_version_only:
			print(f'Branch {source_branch_name} has been created, exiting...')
			return

		update_golang_org_x_commit: Optional[GitCommit] = self.update_golang_org_x(commit)
		if isinstance(update_golang_org_x_commit, GitCommit):
			sha: str = update_golang_org_x_commit.sha
			self.update_branch(source_branch_name, sha)

		self.create_pr(self.latest_go_version, commit_message, get_repo_owner(), source_branch_name,
			target_branch)

	def get_repo(self, repo_name: str) -> Repository:
		"""
		Fetches a PyGitHub Repository object using the passed repository name.
		"""
		try:
			repo: Repository = self.gh_api.get_repo(repo_name)
		except BadCredentialsException as e:
			raise PermissionError(f"Credentials from token '{ENV_GITHUB_TOKEN}' were bad") from e
		return repo

	def get_go_milestone(self, go_version: str) -> Optional[str]:
		"""
		Gets a URL for the GitHub milestone that tracks the release of the
		passed Go version.

		If the passed version is not found to have a milestone associated with
		it, an exception is raised.
		"""
		go_repo: Repository = self.get_repo(GO_REPO_NAME)
		milestones = go_repo.get_milestones(state='all', sort='due_on', direction='desc')
		milestone_title = f'Go{go_version}'
		for milestone in milestones:  # type: Milestone
			if milestone.title == milestone_title:
				print(f'Found Go milestone {milestone.title}')
				return milestone.raw_data.get('html_url')
		raise Exception(f'Could not find a milestone named {milestone_title}.')

	def file_contents(self, file: str, branch: str = "master") -> ContentFile:
		"""
		Gets the contents of the given file path within the repository,
		optionally on a specific branch ("master" by default).

		All trailing whitespace (e.g. extra newlines) is stripped.

		An exception is raised if ``file`` is not a path to a regular file,
		relative to the root of the repository (on the given branch).
		"""
		contents = self.repo.get_contents(file, f"refs/heads/{branch}")
		if isinstance(contents, list):
			raise IsADirectoryError(f"cannot get file contents of '{file}': is a directory")
		return contents

	def get_repo_go_version(self, branch: str = 'master') -> str:
		"""
		Gets the current Go version used at the head of the given branch (or not
		given to use "master" by default) for the repository.
		"""
		return self.file_contents(getenv(ENV_GO_VERSION_FILE), branch).decoded_content.decode()

	def set_go_version(self, go_version: str, commit_message: str,
			source_branch_name: str) -> Commit:
		"""
		Makes the commits necessary to change the Go version used by the
		repository.

		This includes updating the GO_VERSION and .env files at the repository's
		root.
		"""
		master: Branch = self.repo.get_branch('master')
		sha = master.commit.sha
		ref = f'refs/heads/{source_branch_name}'
		self.repo.create_git_ref(ref, sha)

		print(f'Created branch {source_branch_name}')
		go_version_file = getenv(ENV_GO_VERSION_FILE)
		kwargs = {
			"branch": source_branch_name,
			"committer": NotSet,
			"content": f"${go_version}\n",
			"path": go_version_file,
			"message": commit_message,
			"sha": self.file_contents(go_version_file, source_branch_name).sha
		}
		try:
			git_author_name = getenv(ENV_GIT_AUTHOR_NAME)
			git_author_email = GIT_AUTHOR_EMAIL_TEMPLATE.format(git_author_name=git_author_name)
			author: InputGitAuthor = InputGitAuthor(name=git_author_name, email=git_author_email)
			kwargs["author"] = author
		except KeyError:
			print('Committing using the default author')

		self.repo.update_file(**kwargs)
		print(f'Updated {go_version_file} on {self.repo.name}')
		env_path = os.path.join(os.path.dirname(go_version_file), ".env")
		kwargs["path"] = env_path
		kwargs["content"] = f"GO_VERSION={go_version}\n"
		kwargs["sha"] = self.file_contents(go_version_file, source_branch_name).sha
		commit: Commit = self.repo.update_file(**kwargs)["commit"]
		print(f"Updated {env_path} on {self.repo.name}")
		return commit

	def update_golang_org_x(self, previous_commit: Commit) -> Optional[GitCommit]:
		"""
		Updates golang.org/x/ Go dependencies as necessary for the new Go
		version.
		"""
		subprocess.run(['git', 'fetch', 'origin'], check=True)
		subprocess.run(['git', 'checkout', previous_commit.sha], check=True)
		subprocess.run([os.path.join(os.path.dirname(__file__), 'update_golang_org_x.sh')], check=True)
		files_to_check: list[str] = ['go.mod', 'go.sum', os.path.join('vendor', 'modules.txt')]
		tree_elements: list[InputGitTreeElement] = []
		for file in files_to_check:
			diff_process = subprocess.run(['git', 'diff', '--exit-code', '--', file], check=False)
			if diff_process.returncode == 0:
				continue
			with open(file, encoding="UTF-8") as stream:
				content: str = stream.read()
			tree_element: InputGitTreeElement = InputGitTreeElement(path=file, mode='100644',
				type='blob', content=content)
			tree_elements.append(tree_element)
		if len(tree_elements) == 0:
			print('No golang.org/x/ dependencies need to be updated.')
			return None
		tree_hash = subprocess.check_output(
			['git', 'log', '-1', '--pretty=%T', previous_commit.sha]).decode().strip()
		base_tree: GitTree = self.repo.get_git_tree(sha=tree_hash)
		tree: GitTree = self.repo.create_git_tree(tree_elements, base_tree)
		commit_message: str = f'Update golang.org/x/ dependencies for go{self.latest_go_version}'
		previous_git_commit: GitCommit = self.repo.get_git_commit(previous_commit.sha)
		git_commit: GitCommit = self.repo.create_git_commit(message=commit_message, tree=tree,
			parents=[previous_git_commit],
			author=self.author, committer=self.author)
		print('Updated golang.org/x/ dependencies')
		return git_commit

	def create_pr(self, latest_go_version: str, commit_message: str, owner: str,
			source_branch_name: str, target_branch: str) -> None:
		"""
		Creates the pull request to update the Go version.
		"""
		prs = self.gh_api.search_issues(
			f'repo:{self.repo.full_name} is:pr is:open head:{source_branch_name}')
		for list_item in prs:
			pull_request = self.repo.get_pull(list_item.number)
			if pull_request.head.ref != source_branch_name:
				continue
			print(f'Pull request for branch {source_branch_name} already exists:\n{pull_request.html_url}')
			return

		milestone_url = self.get_go_milestone(latest_go_version)
		if milestone_url is None:
			#TODO subclass this
			raise LookupError(f"no milestone found for '${latest_go_version}'")
		pr_body = get_pr_body(latest_go_version, milestone_url)
		pull_request: PullRequest = self.repo.create_pull(
			title=commit_message,
			body=pr_body,
			head=f'{owner}:{source_branch_name}',
			base=target_branch,
			maintainer_can_modify=True,
		)
		try:
			go_version_label: Label = self.repo.get_label('go version')
			pull_request.add_to_labels(go_version_label)
		except UnknownObjectException:
			print('Unable to find a label named "go version"', file=sys.stderr)
		print(f'Created pull request {pull_request.html_url}')