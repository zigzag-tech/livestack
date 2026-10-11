"""Immutable provider entry; operator settings come from private ZZOPS registration."""
import argparse
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

from .provider_installation import verify_installation, sha, private_config, provider_environment, validate_provider_startup
from .provider_lifecycle import ProviderLifecycle
from .provider_owner_socket import ProviderOwnerSocket
from .provider_owner_probe import process_start_ticks




def configure_qualification(config_path, app, module, owner, settings, identity):
    """Installed source entry derives dynamic identity; private data grants none."""
    qualification = settings.get('qualificationOnly',False)
    if type(qualification) is not bool:
        raise ValueError('explicit_qualification_mode_required')
    if not qualification:
        return None
    config,digest = private_config(config_path)
    selected = [entry for entry in config.get('apps',[]) if entry.get('descriptor',{}).get('app')==app]
    if len(selected)!=1 or digest!=identity['configurationDigest']:
        raise ValueError('qualification_source_configuration_changed')
    node = selected[0].get('providerQualificationNode')
    validate_provider_startup(selected[0], app, config_path)
    from .provider_qualification import QualificationClient
    from .qualification_node import QualificationNodeGate
    from .qualification_reservations import QualificationReservationReader
    client = QualificationClient(node['authorizationClient'],identity)
    invocation = os.environ.get('INVOCATION_ID','')
    if len(invocation)!=32 or any(value not in '0123456789abcdef' for value in invocation):
        raise ValueError('qualification_actual_unit_invocation_required')
    binding = {key:node[key] for key in ('nodeId','authority','deviceId','unitKind','hostId')}
    binding.update(identitySha256=client.source_digest,configurationSha256=digest,
        serverInstanceId=owner.instance_id,bootId=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        unitInvocationId=invocation,processStartTicks=str(process_start_ticks(os.getpid())))
    gate = QualificationNodeGate(module.manager,client,QualificationReservationReader(node['reservationPath']),binding,
        owner_socket=node['ownerSocket'],unit_sha256=node['unitSha256'],capacity_url=node['capacityUrl'],nvml_sha256=node['nvmlSha256'],activation_store=node['activationStore'])
    module.configure_provider_qualification(gate)
    return gate


def verify_entry_module(source_root, environment_root, entry_file):
    """Isolated -m resolves installed code; bind it to declared source bytes."""
    source = Path(source_root)/'livestack/node-py/livestack_node/provider_entry.py'
    actual = Path(entry_file).resolve(strict=True)
    if actual == source.resolve(strict=True):
        return
    environment = Path(environment_root).resolve(strict=True)
    if not actual.is_relative_to(environment) or actual.name != 'provider_entry.py' or actual.parent.name != 'livestack_node' or sha(actual.read_bytes()) != sha(source.read_bytes()):
        raise ValueError('installed_framework_entry_path_or_bytes_mismatch')


def serve(config_path, app):
    settings, identity = verify_installation(config_path, app)
    root = Path(settings['sourceRoot'])
    framework = root/'livestack/node-py'
    verify_entry_module(root,sys.prefix,__file__)
    holder = settings['startupHolder']
    environment = provider_environment(settings, app)
    config_file = settings.get('backendConfig')
    if settings.get('qualificationOnly',False) is True:
        config,digest = private_config(config_path)
        nodes=[entry.get('providerQualificationNode') for entry in config.get('apps',[]) if entry.get('descriptor',{}).get('app')==app]
        if len(nodes)!=1 or not isinstance(nodes[0],dict) or digest!=identity['configurationDigest']:
            raise ValueError('qualification_installed_node_required_before_import')
        node=nodes[0]
        from .qualification_device import qualification_cuda_device
        device=qualification_cuda_device(environment.get('CUDA_VISIBLE_DEVICES'),node.get('deviceId'),node.get('nvmlSha256'))
        cache=Path(node.get('activationStore',''))
        if cache.name!='qualification-activation.json' or cache.parent!=Path(settings['controlSocket']).parent or not cache.is_absolute() or cache.resolve()!=cache or cache.parent.stat().st_uid!=os.getuid() or cache.parent.stat().st_mode&0o077:
            raise ValueError('qualification_private_activation_store_required')
        environment[app.upper()+'_QUALIFICATION_DEVICE_ID']=device
        environment[app.upper()+'_QUALIFICATION_ACTIVATION_STORE']=str(cache)
    os.environ.update(environment)
    service_root = root/app/('cuda' if app == 'polyasr' else '')
    sys.path[:0] = [str(service_root),str(root/app),str(framework)]
    saved_argv = sys.argv
    sys.argv = [str(service_root/'server.py')] + (['--config',config_file] if config_file else [])
    try:
        spec = importlib.util.spec_from_file_location('zz_owned_provider_server',service_root/'server.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.argv = saved_argv
    import uvicorn
    def synchronize():
        if app == 'polyasr':
            module._synchronize_owned_alignment()
        elif module.RUNTIME == 'mlx':
            import mlx.core as mx
            mx.synchronize()
        else:
            module._synchronize_owned_synthesis()
    owner = ProviderOwnerSocket(settings['controlSocket'],config_path,identity,lambda:None,synchronize)
    fence = module.configure_provider_fence(owner.authorize,identity)
    fence.hold(owner._credential,holder)
    server = uvicorn.Server(uvicorn.Config(module.app,host=settings['host'],port=settings['port']))
    async def unload():
        loop = asyncio.get_running_loop()
        if module.manager is None:
            raise RuntimeError('managed_provider_shutdown_not_available')
        def owned_unload():
            gate = getattr(module.app.state,'qualification_gate',None)
            if gate is not None:gate.settle_uncertain_owned(fence,synchronize)
            return module.manager.unload_now()
        if app == 'polyasr':
            await loop.run_in_executor(module._legacy_executor,module._gpu_call,owned_unload)
        else:
            await loop.run_in_executor(module._gpu_executor,owned_unload)
    lifecycle = ProviderLifecycle(module.app,owner,fence,holder,server,synchronize,unload)
    owner.shutdown = lifecycle.request_shutdown
    owner.start(fence)
    try:
        configure_qualification(config_path,app,module,owner,settings,identity)
        server.run()
    finally:
        # Unknown/failed lifecycle effects retain local ownership. They cannot
        # be converted into an acknowledged successful stop or automatic rerun.
        if lifecycle._shutdown_error is not None or not lifecycle._shutdown_done.is_set():
            raise RuntimeError('provider_lifecycle_held_requires_operator_reconciliation')
        owner.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--private-service-config',required=True)
    parser.add_argument('--app',choices=('polytts','polyasr'),required=True)
    args = parser.parse_args()
    serve(args.private_service_config,args.app)


if __name__ == '__main__':
    main()
