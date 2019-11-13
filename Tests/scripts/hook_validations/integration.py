import os
import re

import requests
import yaml

from Tests.scripts.constants import CONTENT_GITHUB_LINK, PYTHON_SUBTYPES, INTEGRATION_CATEGORIES, INTEGRATION_PY_REGEX, \
    INTEGRATION_YML_REGEX, PACKS_INTEGRATION_PY_REGEX, PACKS_INTEGRATION_YML_REGEX, INTEGRATION_REGEX, \
    BETA_INTEGRATION_REGEX, BETA_INTEGRATION_YML_REGEX
from Tests.scripts.hook_validations.error_constants import Errors
from Tests.test_utils import print_error, get_yaml, print_warning, server_version_compare, get_release_notes_file_path, \
    get_latest_release_notes_text
from yml_based import YMLBasedValidator

# disable insecure warnings
requests.packages.urllib3.disable_warnings()


class IntegrationValidator(YMLBasedValidator):
    """IntegrationValidator is designed to validate the correctness of the file structure we enter to content repo. And
    also try to catch possible Backward compatibility breaks due to the preformed changes.

    Attributes:
       _is_valid (bool): the attribute which saves the valid/in-valid status of the current file.
       file_path (str): the path to the file we are examining at the moment.
       current_integration (dict): Json representation of the current integration from the branch.
       old_integration (dict): Json representation of the current integration from master.
    """

    regexes = [
        INTEGRATION_PY_REGEX,
        INTEGRATION_YML_REGEX,
        PACKS_INTEGRATION_PY_REGEX,
        PACKS_INTEGRATION_YML_REGEX,
        INTEGRATION_REGEX,
        BETA_INTEGRATION_REGEX,
        BETA_INTEGRATION_YML_REGEX
    ]

    def __init__(self, file_path, check_git=True, old_file_path=None, old_git_branch='master', is_added_file=False,
                 is_renamed=False):
        super(IntegrationValidator, self).__init__(file_path, is_added_file=is_added_file, is_renamed=is_renamed)
        self._is_valid = True

        self.file_path = file_path
        if check_git:
            self.current_integration = get_yaml(file_path)
            # The replace in the end is for Windows support
            if old_file_path:
                git_hub_path = os.path.join(CONTENT_GITHUB_LINK, old_git_branch, old_file_path).replace("\\", "/")
                file_content = requests.get(git_hub_path, verify=False).content
                self.old_integration = yaml.safe_load(file_content)
            else:
                try:
                    file_path_from_old_branch = os.path.join(CONTENT_GITHUB_LINK, old_git_branch, file_path).replace(
                        "\\", "/")
                    res = requests.get(file_path_from_old_branch, verify=False)
                    res.raise_for_status()
                    self.old_integration = yaml.safe_load(res.content)
                except Exception as e:
                    print_warning(Errors.breaking_backwards_no_old_script(e))
                    self.old_integration = None

    def is_backward_compatible(self):
        """Check whether the Integration is backward compatible or not, update the _is_valid field to determine that"""
        if not self.old_integration:
            return True

        self.is_context_path_changed()
        self.is_docker_image_changed()
        self.is_added_required_fields()
        self.is_changed_command_name_or_arg()
        self.is_there_duplicate_args()
        self.is_there_duplicate_params()
        self.is_changed_subtype()

        # will move to is_valid_integration after https://github.com/demisto/etc/issues/17949
        self.is_outputs_for_reputations_commands_valid()
        return self._is_valid

    def is_file_valid(self):
        """Check whether the Integration is valid or not, update the _is_valid field to determine that"""
        self.is_valid_subtype()
        self.is_default_arguments()
        self.is_proxy_configured_correctly()
        self.is_insecure_configured_correctly()
        self.is_valid_category()

        return self._is_valid

    def is_valid_beta_integration(self, is_new=False):
        """Check whether the beta Integration is valid or not, update the _is_valid field to determine that"""
        self.is_default_arguments()
        self.is_valid_beta(is_new)
        return self._is_valid

    def is_valid_param(self, param_name, param_display):
        """Check if the given parameter has the right configuration."""
        err_msgs = []
        configuration = self.current_integration.get('configuration', [])
        for configuration_param in configuration:
            configuration_param_name = configuration_param['name']
            if configuration_param_name == param_name:
                if configuration_param['display'] != param_display:
                    err_msgs.append('The display name of the {} parameter should be \'{}\''.format(param_name,
                                                                                                   param_display))
                elif configuration_param.get('defaultvalue', '') != 'false' and configuration_param.get('defaultvalue',
                                                                                                        '') != '':
                    err_msgs.append('The default value of the {} parameter should be \'\''.format(param_name))

                elif configuration_param.get('required', False):
                    err_msgs.append('The required field of the {} parameter should be False'.format(param_name))

                elif configuration_param.get('type') != 8:
                    err_msgs.append('The type field of the {} parameter should be 8'.format(param_name))

        if err_msgs:
            print_error('Received the following error for {} validation:\n{}'.format(param_name, '\n'.join(err_msgs)))
            self._is_valid = False
            return False

        return True

    def is_proxy_configured_correctly(self):
        """Check that if an integration has a proxy parameter that it is configured properly."""
        return self.is_valid_param('proxy', 'Use system proxy settings')

    def is_insecure_configured_correctly(self):
        """Check that if an integration has a insecure parameter that it is configured properly."""
        insecure_field_name = ''
        configuration = self.current_integration.get('configuration', [])
        for configuration_param in configuration:
            if configuration_param['name'] == 'insecure' or configuration_param['name'] == 'unsecure':
                insecure_field_name = configuration_param['name']

        if insecure_field_name:
            return self.is_valid_param(insecure_field_name, 'Trust any certificate (not secure)')

    def is_valid_category(self):
        """Check that the integration category is in the schema."""
        category = self.current_integration.get('category', None)
        if not category or category not in INTEGRATION_CATEGORIES:
            self._is_valid = False
            print_error("The category '{}' is not in the integration schemas, the valid options are:\n{}".format(
                category, '\n'.join(INTEGRATION_CATEGORIES)))

        return self._is_valid

    def is_default_arguments(self):
        """Check if a reputation command (domain/email/file/ip/url)
            has a default non required argument with the same name

        Returns:
            bool. Whether a reputation command hold a valid argument
        """
        commands = self.current_integration.get('script', {}).get('commands', [])
        for command in commands:
            command_name = command.get('name')
            for arg in command.get('arguments', []):
                arg_name = arg.get('name')
                if ((command_name == 'file' and arg_name == 'file')
                        or (command_name == 'email' and arg_name == 'email')
                        or (command_name == 'domain' and arg_name == 'domain')
                        or (command_name == 'url' and arg_name == 'url')
                        or (command_name == 'ip' and arg_name == 'ip')):
                    if arg.get('default') is False:
                        self._is_valid = False
                        print_error("The argument '{}' of the command '{}' is not configured as default"
                                    .format(arg_name, command_name))
        return self._is_valid

    def is_outputs_for_reputations_commands_valid(self):
        """Check if a reputation command (domain/email/file/ip/url)
            has the correct DBotScore outputs according to the context standard
            https://github.com/demisto/content/blob/master/docs/context_standards/README.MD

        Returns:
            bool. Whether a reputation command holds valid outputs
        """
        context_standard = "https://github.com/demisto/content/blob/master/docs/context_standards/README.MD"
        commands = self.current_integration.get('script', {}).get('commands', [])
        for command in commands:
            command_name = command.get('name')
            # look for reputations commands
            if command_name in ['domain', 'email', 'file', 'ip', 'url']:
                context_outputs_paths = set()
                context_outputs_descriptions = set()
                for output in command.get('outputs', []):
                    context_outputs_paths.add(output.get('contextPath'))
                    context_outputs_descriptions.add(output.get('description'))

                # validate DBotScore outputs and descriptions
                DBot_Score = {
                    'DBotScore.Indicator': 'The indicator that was tested.',
                    'DBotScore.Type': 'The indicator type.',
                    'DBotScore.Vendor': 'The vendor used to calculate the score.',
                    'DBotScore.Score': 'The actual score.'
                }
                missing_outputs = set()
                missing_descriptions = set()
                for DBot_Score_output in DBot_Score:
                    if DBot_Score_output not in context_outputs_paths:
                        missing_outputs.add(DBot_Score_output)
                        self._is_valid = False
                    else:  # DBot Score output path is in the outputs
                        if DBot_Score.get(DBot_Score_output) not in context_outputs_descriptions:
                            missing_descriptions.add(DBot_Score_output)
                            # self._is_valid = False - Do not fail build over wrong description

                if missing_outputs:
                    print_error(Errors.missing_outputs(command_name, missing_outputs, context_standard))
                if missing_descriptions:
                    print_warning(
                        Errors.missing_dbot_description(command_name, missing_descriptions, context_standard))

                # validate the IOC output
                command_to_output = {
                    'domain': {'Domain.Name'},
                    'file': {'File.MD5', 'File.SHA1', 'File.SHA256'},
                    'ip': {'IP.Address'},
                    'url': {'URL.Data'}
                }
                reputation_output = command_to_output.get(command_name)
                if reputation_output and not reputation_output.intersection(context_outputs_paths):
                    self._is_valid = False
                    print_error(Errors.missing_reputation(command_name, reputation_output, context_standard))

        return self._is_valid

    def is_valid_subtype(self):
        """Validate that the subtype is python2 or python3."""
        type_ = self.current_integration.get('script', {}).get('type')
        if type_ == 'python':
            subtype = self.current_integration.get('script', {}).get('subtype')
            if subtype not in PYTHON_SUBTYPES:
                print_error(Errors.wrong_subtype(self.current_integration.gey('name')))
                self._is_valid = False
        return self._is_valid

    def is_changed_subtype(self):
        """Validate that the subtype was not changed."""
        type_ = self.current_integration.get('script', {}).get('type')
        if type_ == 'python':
            subtype = self.current_integration.get('script', {}).get('subtype')
            if self.old_integration:
                old_subtype = self.old_integration.get('script', {}).get('subtype', "")
                if old_subtype and old_subtype != subtype:
                    print_error(Errors.breaking_backwards_subtype(self.file_path))
                    self._is_valid = False

        return self._is_valid

    def is_valid_beta(self, is_new=False):
        """Validate that that beta integration has correct beta attributes"""

        if not all([self._is_display_contains_beta(), self._has_beta_param()]):
            self._is_valid = False
        if is_new:
            if not all([self._id_has_no_beta_substring(), self._name_has_no_beta_substring()]):
                self._is_valid = False

    def _id_has_no_beta_substring(self):
        """Checks that 'id' field dose not include the substring 'beta'"""
        common_fields = self.current_integration.get('commonfields', {})
        integration_id = common_fields.get('id', '')
        if 'beta' in integration_id.lower():
            print_error(Errors.beta_in_id(self.file_path))
            return False
        return True

    def _name_has_no_beta_substring(self):
        """Checks that 'name' field dose not include the substring 'beta'"""
        name = self.current_integration.get('name', '')
        if 'beta' in name.lower():
            print_error(Errors.beta_in_name(self.file_path))
            return False
        return True

    def _has_beta_param(self):
        """Checks that integration has 'beta' field with value set to true"""
        beta = self.current_integration.get('beta', False)
        if not beta:
            print_error("Beta integration yml file should have the field \"beta: true\", but was not found"
                        " in the file {}".format(self.file_path))
        return beta

    def _is_display_contains_beta(self):
        """Checks that 'display' field includes the substring 'beta'"""
        display = self.current_integration.get('display', '')
        if 'beta' not in display.lower():
            print_error(Errors.no_beta_in_display(self.file_path))
            return False
        return True

    def is_there_duplicate_args(self):
        """Check if a command has the same arg more than once

        Returns:
            bool. True if there are duplicates, False otherwise.
        """
        commands = self.current_integration.get('script', {}).get('commands', [])
        for command in commands:
            duplicates = self.find_duplicates(command.get('arguments', []))
            if duplicates:
                self._is_valid = False
                print_error(
                    Errors.duplicate_arg_in_integration(duplicates, command['name'],
                                                        self.current_integration.get('name')))
        return not self._is_valid

    def is_there_duplicate_params(self):
        """Check if the integration has the same param more than once

        Returns:
            bool. True if there are duplicates, False otherwise.
        """
        configurations = self.current_integration.get('configuration', [])
        param_list = [configuration_param['name'] for configuration_param in configurations]
        duplicates = self.find_duplicates(param_list)
        if duplicates:
            self._is_valid = False
            print_error(Errors.duplicate_param(duplicates, self.current_integration))
        return not self._is_valid

    def _get_command_to_args(self, integration_json):
        """Get a dictionary command name to it's arguments.

        Args:
            integration_json (dict): Dictionary of the examined integration.

        Returns:
            dict. command name to a list of it's arguments.
        """
        command_to_args = {}
        commands = integration_json.get('script', {}).get('commands', [])
        for command in commands:
            command_to_args[command['name']] = {}
            for arg in command.get('arguments', []):
                command_to_args[command['name']][arg['name']] = arg.get('required', False)
        return command_to_args

    def is_changed_command_name_or_arg(self):
        """Check if a command name or argument as been changed.

        Returns:
            bool. Whether a command name or argument as been changed.
        """
        current_command_to_args = self._get_command_to_args(self.current_integration)
        old_command_to_args = self._get_command_to_args(self.old_integration)

        for command, args_dict in old_command_to_args.items():
            if command not in current_command_to_args.keys() or \
                    not self.is_subset_dictionary(current_command_to_args[command], args_dict):
                print_error(Errors.breaking_backwards_command_arg_changed(self.file_path, command))
                self._is_valid = False
                return True

        return False

    @classmethod
    def _get_command_to_context_paths(cls, integration_json):
        """Get a dictionary command name to it's context paths.

        Args:
            integration_json (dict): Dictionary of the examined integration.

        Returns:
            dict. command name to a list of it's context paths.
        """
        command_to_context_list = {}
        commands = integration_json.get('script', {}).get('commands', [])
        for command in commands:
            context_list = []
            if not command.get('outputs', []):
                continue

            for output in command.get('outputs', []):
                command_name = command['name']
                try:
                    context_list.append(output['contextPath'])
                except KeyError:
                    print('Invalid context output for command {}. Output is {}'.format(command_name, output))

            command_to_context_list[command['name']] = sorted(context_list)

        return command_to_context_list

    def is_context_path_changed(self):
        """Check if a context path as been changed.

        Returns:
            bool. Whether a context path as been changed.
        """
        current_command_to_context_paths = self._get_command_to_context_paths(self.current_integration)
        old_command_to_context_paths = self._get_command_to_context_paths(self.old_integration)

        for old_command, old_context_paths in old_command_to_context_paths.items():
            if old_command in current_command_to_context_paths.keys() and \
                    not self._is_sub_set(current_command_to_context_paths[old_command],
                                         old_context_paths):
                print_error(Errors.breaking_backwards_command(self.file_path, old_command))
                self._is_valid = False
                return True

        return False

    def get_arg_to_required_dict(self, integration_json, args='configuration'):
        """Get a dictionary field name to its required status.

        Args:
            integration_json (dict): Dictionary of the examined integration.
            args:

        Returns:
            dict. Field name to its required status.
        """
        return super(IntegrationValidator, self).get_arg_to_required_dict(integration_json, args)

    def is_added_required_fields(self):
        """Check if required field were added."""
        current_field_to_required = self.get_arg_to_required_dict(self.current_integration)
        old_field_to_required = self.get_arg_to_required_dict(self.old_integration)

        for field, required in current_field_to_required.items():
            if (field not in old_field_to_required.keys() and required) or \
                    (required and field in old_field_to_required.keys() and required != old_field_to_required[field]):
                print_error(Errors.added_required_fields(self.file_path, field))
                self._is_valid = False
                return True
        return False

    def is_docker_image_changed(self):
        """Check if the Docker image was changed or not."""
        # Unnecessary to check docker image only on 5.0 and up
        if server_version_compare(self.old_integration.get('fromversion', '0'), '5.0.0') < 0:
            if self.old_integration.get('script', {}).get('dockerimage', "") != \
                    self.current_integration.get('script', {}).get('dockerimage', ""):
                print_error(Errors.breaking_backwards_docker(self.file_path))
                self._is_valid = False
                return True
        return False

    def _is_beta_integration(self):
        """Checks if file is under Beta_integration dir"""
        return re.match(BETA_INTEGRATION_REGEX, self.file_path, re.IGNORECASE) or \
               re.match(BETA_INTEGRATION_YML_REGEX, self.file_path, re.IGNORECASE) or \
               re.match(PACKS_INTEGRATION_PY_REGEX, self.file_path, re.IGNORECASE)

    def validate_file_release_notes(self):
        """Validate that the file has proper release notes when modified.

        This function updates the class attribute self._is_valid instead of passing it back and forth.
        """
        if self.is_renamed:
            print_warning("You might need RN please make sure to check that.")
            return

        if os.path.isfile(self.file_path):
            rn_path = get_release_notes_file_path(self.file_path)
            rn = get_latest_release_notes_text(rn_path)

            # check rn file exists and contain text
            if rn is None:
                print_error('File {} is missing releaseNotes, Please add it under {}'.format(self.file_path, rn_path))
                self._is_valid = False
