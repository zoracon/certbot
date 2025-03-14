"""Facilities for implementing hooks that call shell commands."""

import logging
from typing import List
from typing import Optional
from typing import Set

from certbot import configuration
from certbot import errors
from certbot import util
from certbot.compat import filesystem
from certbot.compat import misc
from certbot.compat import os
from certbot.display import ops as display_ops
from certbot.plugins import util as plug_util

logger = logging.getLogger(__name__)


def validate_hooks(config: configuration.NamespaceConfig) -> None:
    """Check hook commands are executable."""
    validate_hook(config.pre_hook, "pre")
    validate_hook(config.post_hook, "post")
    validate_hook(config.deploy_hook, "deploy")
    validate_hook(config.renew_hook, "renew")


def _prog(shell_cmd: str) -> Optional[str]:
    """Extract the program run by a shell command.

    :param str shell_cmd: command to be executed

    :returns: basename of command or None if the command isn't found
    :rtype: str or None

    """
    if not util.exe_exists(shell_cmd):
        plug_util.path_surgery(shell_cmd)
        if not util.exe_exists(shell_cmd):
            return None

    return os.path.basename(shell_cmd)


def validate_hook(shell_cmd: str, hook_name: str) -> None:
    """Check that a command provided as a hook is plausibly executable.

    :raises .errors.HookCommandNotFound: if the command is not found
    """
    if shell_cmd:
        cmd = shell_cmd.split(None, 1)[0]
        if not _prog(cmd):
            path = os.environ["PATH"]
            if os.path.exists(cmd):
                msg = f"{cmd}-hook command {hook_name} exists, but is not executable."
            else:
                msg = (
                    f"Unable to find {hook_name}-hook command {cmd} in the PATH.\n(PATH is {path})"
                )

            raise errors.HookCommandNotFound(msg)


def pre_hook(config: configuration.NamespaceConfig) -> None:
    """Run pre-hooks if they exist and haven't already been run.

    When Certbot is running with the renew subcommand, this function
    runs any hooks found in the config.renewal_pre_hooks_dir (if they
    have not already been run) followed by any pre-hook in the config.
    If hooks in config.renewal_pre_hooks_dir are run and the pre-hook in
    the config is a path to one of these scripts, it is not run twice.

    :param configuration.NamespaceConfig config: Certbot settings

    """
    if config.verb == "renew" and config.directory_hooks:
        for hook in list_hooks(config.renewal_pre_hooks_dir):
            _run_pre_hook_if_necessary(hook)

    cmd = config.pre_hook
    if cmd:
        _run_pre_hook_if_necessary(cmd)


executed_pre_hooks: Set[str] = set()


def _run_pre_hook_if_necessary(command: str) -> None:
    """Run the specified pre-hook if we haven't already.

    If we've already run this exact command before, a message is logged
    saying the pre-hook was skipped.

    :param str command: pre-hook to be run

    """
    if command in executed_pre_hooks:
        logger.info("Pre-hook command already run, skipping: %s", command)
    else:
        _run_hook("pre-hook", command)
        executed_pre_hooks.add(command)


def post_hook(config: configuration.NamespaceConfig) -> None:
    """Run post-hooks if defined.

    This function also registers any executables found in
    config.renewal_post_hooks_dir to be run when Certbot is used with
    the renew subcommand.

    If the verb is renew, we delay executing any post-hooks until
    :func:`run_saved_post_hooks` is called. In this case, this function
    registers all hooks found in config.renewal_post_hooks_dir to be
    called followed by any post-hook in the config. If the post-hook in
    the config is a path to an executable in the post-hook directory, it
    is not scheduled to be run twice.

    :param configuration.NamespaceConfig config: Certbot settings

    """

    cmd = config.post_hook
    # In the "renew" case, we save these up to run at the end
    if config.verb == "renew":
        if config.directory_hooks:
            for hook in list_hooks(config.renewal_post_hooks_dir):
                _run_eventually(hook)
        if cmd:
            _run_eventually(cmd)
    # certonly / run
    elif cmd:
        _run_hook("post-hook", cmd)


post_hooks: List[str] = []


def _run_eventually(command: str) -> None:
    """Registers a post-hook to be run eventually.

    All commands given to this function will be run exactly once in the
    order they were given when :func:`run_saved_post_hooks` is called.

    :param str command: post-hook to register to be run

    """
    if command not in post_hooks:
        post_hooks.append(command)


def run_saved_post_hooks() -> None:
    """Run any post hooks that were saved up in the course of the 'renew' verb"""
    for cmd in post_hooks:
        _run_hook("post-hook", cmd)


def deploy_hook(config: configuration.NamespaceConfig, domains: List[str],
                lineage_path: str) -> None:
    """Run post-issuance hook if defined.

    :param configuration.NamespaceConfig config: Certbot settings
    :param domains: domains in the obtained certificate
    :type domains: `list` of `str`
    :param str lineage_path: live directory path for the new cert

    """
    if config.deploy_hook:
        _run_deploy_hook(config.deploy_hook, domains,
                         lineage_path, config.dry_run, config.run_deploy_hooks)


def renew_hook(config: configuration.NamespaceConfig, domains: List[str],
               lineage_path: str) -> None:
    """Run post-renewal hooks.

    This function runs any hooks found in
    config.renewal_deploy_hooks_dir followed by any renew-hook in the
    config. If the renew-hook in the config is a path to a script in
    config.renewal_deploy_hooks_dir, it is not run twice.

    If Certbot is doing a dry run, no hooks are run and messages are
    logged saying that they were skipped.

    :param configuration.NamespaceConfig config: Certbot settings
    :param domains: domains in the obtained certificate
    :type domains: `list` of `str`
    :param str lineage_path: live directory path for the new cert

    """
    executed_dir_hooks = set()
    if config.directory_hooks:
        for hook in list_hooks(config.renewal_deploy_hooks_dir):
            _run_deploy_hook(hook, domains, lineage_path, config.dry_run, config.run_deploy_hooks)
            executed_dir_hooks.add(hook)

    if config.renew_hook:
        if config.renew_hook in executed_dir_hooks:
            logger.info("Skipping deploy-hook '%s' as it was already run.",
                        config.renew_hook)
        else:
            _run_deploy_hook(config.renew_hook, domains,
                             lineage_path, config.dry_run, config.run_deploy_hooks)


def _run_deploy_hook(command: str, domains: List[str], lineage_path: str, dry_run: bool,
                     run_deploy_hooks: bool) -> None:
    """Run the specified deploy-hook (if not doing a dry run).

    If dry_run is True, command is not run and a message is logged
    saying that it was skipped. If dry_run is False, the hook is run
    after setting the appropriate environment variables.

    :param str command: command to run as a deploy-hook
    :param domains: domains in the obtained certificate
    :type domains: `list` of `str`
    :param str lineage_path: live directory path for the new cert
    :param bool dry_run: True iff Certbot is doing a dry run
    :param bool run_deploy_hooks: True if deploy hooks should run despite Certbot doing a dry run

    """
    if dry_run and not run_deploy_hooks:
        logger.info("Dry run: skipping deploy hook command: %s",
                       command)
        return

    os.environ["RENEWED_DOMAINS"] = " ".join(domains)
    os.environ["RENEWED_LINEAGE"] = lineage_path
    _run_hook("deploy-hook", command)


def _run_hook(cmd_name: str, shell_cmd: str) -> str:
    """Run a hook command.

    :param str cmd_name: the user facing name of the hook being run
    :param shell_cmd: shell command to execute
    :type shell_cmd: `list` of `str` or `str`

    :returns: stderr if there was any"""
    returncode, err, out = misc.execute_command_status(
        cmd_name, shell_cmd, env=util.env_no_snap_for_external_calls())
    display_ops.report_executed_command(f"Hook '{cmd_name}'", returncode, out, err)
    return err


def list_hooks(dir_path: str) -> List[str]:
    """List paths to all hooks found in dir_path in sorted order.

    :param str dir_path: directory to search

    :returns: `list` of `str`
    :rtype: sorted list of absolute paths to executables in dir_path

    """
    allpaths = (os.path.join(dir_path, f) for f in os.listdir(dir_path))
    hooks = [path for path in allpaths if filesystem.is_executable(path) and not path.endswith('~')]
    return sorted(hooks)
