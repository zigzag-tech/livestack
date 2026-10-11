"""CPU environment policy controls; no provider import, model or unit effect."""
import unittest
from livestack_node.provider_entry import provider_environment


class QualificationEntryTests(unittest.TestCase):
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
