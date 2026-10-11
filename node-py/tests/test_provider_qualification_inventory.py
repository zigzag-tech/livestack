"""CPU byte inventory fixtures only; no node, model, kernel seal or grants."""
import json
import tempfile
import unittest
from pathlib import Path
from livestack_node.provider_installation import sha, verify_qualification_client_inventory


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',', ':')).encode()


class QualificationInventoryTests(unittest.TestCase):
    def test_default_three_component_contract_and_missing_qualification_refusal(self):
        verify_qualification_client_inventory('/unused',{}, {},{})
        with self.assertRaisesRegex(ValueError,'client_inventory_required'):
            verify_qualification_client_inventory('/unused',{}, {},{'qualificationOnly':True})
        with self.assertRaisesRegex(ValueError,'qualification_only'):
            verify_qualification_client_inventory('/unused',{'authorizationClient':{}},{},{})

    def test_exact_source_bundle_node_binding_and_changed_membership_refusals(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_root=Path(temporary);root=source_root/'authorization-client'
            (root/'bundle').mkdir(parents=True);(root/'sdk').mkdir()
            (root/'node').write_bytes(b'explicit CPU fake executable, never executed')
            (root/'bundle/client.mjs').write_bytes(b'CPU output')
            (root/'bundle/client-library.mjs').write_bytes(b'CPU library')
            (root/'sdk/source.mjs').write_bytes(b'CPU source')
            node_sha=sha((root/'node').read_bytes())
            manifest={'outputs':[{'path':'client.mjs'},{'path':'client-library.mjs'}],'sourceInputs':[{'path':'source.mjs'}],'toolchain':{'node':{'sha256':node_sha}}}
            manifest_bytes=canonical(manifest);(root/'bundle/manifest.json').write_bytes(manifest_bytes)
            files=[{'path':str(p.relative_to(root)),'bytes':p.stat().st_size,'sha256':sha(p.read_bytes())} for p in sorted(root.rglob('*')) if p.is_file()]
            inventory={'files':files,'inventoryDigest':sha(canonical(files)),'manifestSha256':sha(manifest_bytes)}
            source={'authorizationClient':inventory}
            client={'manifestPath':str(root/'bundle/manifest.json'),'manifestSha256':inventory['manifestSha256'],'inputRoot':str(root/'sdk'),'nodeExecutable':str(root/'node'),'nodeSha256':node_sha}
            selected={'providerQualificationNode':{'authorizationClient':client}}
            settings={'qualificationOnly':True}
            verify_qualification_client_inventory(str(source_root),source,selected,settings)
            for field in ('nodeExecutable','inputRoot','manifestPath','manifestSha256'):
                changed={**client,field:client[field]+'changed'}
                with self.assertRaisesRegex(ValueError,'binding_changed'):
                    verify_qualification_client_inventory(str(source_root),source,{'providerQualificationNode':{'authorizationClient':changed}},settings)
            extra={'path':'extra.mjs','bytes':1,'sha256':sha(b'x')};(root/'extra.mjs').write_bytes(b'x')
            changed={**inventory,'files':files+[extra],'inventoryDigest':sha(canonical(files+[extra]))}
            with self.assertRaisesRegex(ValueError,'membership_changed'):
                verify_qualification_client_inventory(str(source_root),{'authorizationClient':changed},selected,settings)
            (root/'node').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'byte_bound|digest_mismatch'):
                verify_qualification_client_inventory(str(source_root),source,selected,settings)
