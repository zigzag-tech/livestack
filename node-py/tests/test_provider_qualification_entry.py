"""CPU environment policy controls; no provider import, model or unit effect."""
import unittest
from livestack_node.provider_entry import provider_environment


class QualificationEntryTests(unittest.TestCase):
    def test_static_startup_checks_run_without_provider_import(self):
        from livestack_node.provider_installation import validate_provider_startup
        from copy import deepcopy
        node = dict.fromkeys(('nodeId','authority','deviceId','unitKind','hostId','capacityUrl','reservationPath','ownerSocket','unitSha256','authorizationClient','nvmlSha256','activationStore'), 'fixture')
        node.update(ownerSocket='/private/owner.sock', authorizationClient={'serviceConfigurationPath': '/private/owner.json'})
        settings = {'startupHolder': 'held-fixture', 'controlSocket': '/private/owner.sock', 'qualificationOnly': True,
                    'environment': {'LIVESTACK_REGISTER': '0', 'LIVESTACK_MESH_ENABLED': '0', 'POLYTTS_QUALIFICATION_ONLY': '1', 'POLYTTS_RUNTIME': 'pytorch', 'POLYTTS_MODELS_DIR': '/private/models'}}
        selected = {'providerActivation': settings, 'providerQualificationNode': node}
        self.assertEqual(validate_provider_startup(selected, 'polytts', '/private/owner.json'), settings['environment'])
        for section, key, value, error in (
            ('providerActivation', 'startupHolder', '', 'startup_identity'),
            ('environment', 'POLYTTS_MODELS_DIR', 'relative', 'model_binding'),
            ('environment', 'POLYTTS_RUNTIME', 'mlx', 'cuda_manager'),
            ('environment', 'POLYTTS_ALLOW_CPU', '1', 'cuda_manager'),
            ('providerQualificationNode', 'ownerSocket', '/other', 'closed_installed_node'),
            ('providerQualificationNode', 'extra', 'value', 'closed_installed_node')):
            changed = deepcopy(selected)
            target = changed['providerActivation']['environment'] if section == 'environment' else changed[section]
            target[key] = value
            with self.assertRaisesRegex(ValueError, error):
                validate_provider_startup(changed, 'polytts', '/private/owner.json')
        with self.assertRaisesRegex(ValueError, 'closed_installed_node'):
            validate_provider_startup(selected, 'polytts', '/other/owner.json')
        import tempfile, hashlib
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'asr.yaml'; path.write_bytes(b'CPU fixture config')
            asr = {'providerActivation': {'startupHolder': 'fixture', 'backendConfig': str(path), 'backendConfigSha256': hashlib.sha256(path.read_bytes()).hexdigest()}}
            validate_provider_startup(asr, 'polyasr', '/private/owner.json')
            asr['providerActivation']['backendConfigSha256'] = 'wrong'
            with self.assertRaisesRegex(ValueError, 'digest_mismatch'):
                validate_provider_startup(asr, 'polyasr', '/private/owner.json')
            alias = Path(directory)/'alias'; alias.symlink_to(path)
            asr['providerActivation']['backendConfig'] = str(alias)
            with self.assertRaisesRegex(ValueError, 'external_asr_config_required'):
                validate_provider_startup(asr, 'polyasr', '/private/owner.json')

    def test_original_environment_unchanged(self):
        for app in ('polytts', 'polyasr'):
            original = {'CUDA_VISIBLE_DEVICES': '1', app.upper()+'_PORT': '8100'}
            self.assertEqual(provider_environment({'environment': original}, app), original)
            with self.assertRaisesRegex(ValueError, 'explicit_provider_environment_invalid'):
                provider_environment({'environment': {**original, 'LIVESTACK_REGISTER': '0'}}, app)

    def test_qualification_flags_disable_only(self):
        for app in ('polytts', 'polyasr'):
            environment = {'CUDA_VISIBLE_DEVICES': '1', 'LIVESTACK_REGISTER': '0',
                           'LIVESTACK_MESH_ENABLED': '0', app.upper()+'_QUALIFICATION_ONLY': '1'}
            settings = {'qualificationOnly': True, 'environment': environment}
            self.assertEqual(provider_environment(settings, app), environment)
            for key in ('LIVESTACK_REGISTER', 'LIVESTACK_MESH_ENABLED'):
                for value in ('1', 'true', '', 'false', 0, False):
                    with self.assertRaisesRegex(ValueError, 'explicit_provider_environment_invalid'):
                        provider_environment({**settings, 'environment': {**environment, key: value}}, app)
                missing = {k: v for k, v in environment.items() if k != key}
                with self.assertRaisesRegex(ValueError, 'qualification_requires_disabled'):
                    provider_environment({**settings, 'environment': missing}, app)
            for value in (None, 1, 'true', [], {}):
                with self.assertRaisesRegex(ValueError, 'explicit_qualification_mode_required'):
                    provider_environment({**settings, 'qualificationOnly': value}, app)
            with self.assertRaisesRegex(ValueError, 'explicit_provider_environment_invalid'):
                provider_environment({**settings, 'environment': {**environment, 'LIVESTACK_NODE_TOKEN': 'untrusted'}}, app)

    def test_marker_cannot_enable_qualification_in_normal_entry(self):
        with self.assertRaisesRegex(ValueError, 'qualification_marker_requires_explicit_mode'):
            provider_environment({'environment': {'POLYTTS_QUALIFICATION_ONLY': '1'}}, 'polytts')

    def test_exact_native_model_key_and_constructor_preflight(self):
        from livestack_node.provider_installation import verify_installation
        from unittest.mock import patch
        settings = {"qualificationOnly": True, "environment": {"LIVESTACK_REGISTER": "0", "LIVESTACK_MESH_ENABLED": "0", "POLYTTS_QUALIFICATION_ONLY": "1", "VOXCPM_MODEL_ID": "/private/sealed/VoxCPM2"}}
        self.assertEqual(provider_environment(settings, "polytts"), settings["environment"])
        for key, value, app in (("VOXCPM_OTHER", "x", "polytts"), ("VOXCPM_MODEL_ID", 1, "polytts"), ("VOXCPM_MODEL_ID", "x", "polyasr")):
            invalid = {"environment": {key: value}}
            with self.assertRaisesRegex(ValueError, "explicit_provider_environment_invalid"):
                provider_environment(invalid, app)
            config = {"apps": [{"descriptor": {"app": app}, "providerActivation": invalid}]}
            with patch("livestack_node.provider_installation.private_config", return_value=(config, "digest")), patch("livestack_node.provider_installation.validate_owner_socket_path") as socket_check:
                with self.assertRaisesRegex(ValueError, "explicit_provider_environment_invalid"):
                    verify_installation("unused", app)
                socket_check.assert_not_called()
