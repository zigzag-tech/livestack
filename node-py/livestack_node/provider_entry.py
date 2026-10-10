"""Immutable provider entry; operator settings come from private ZZOPS registration."""
import argparse
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

from .provider_installation import verify_installation, sha
from .provider_lifecycle import ProviderLifecycle
from .provider_owner_socket import ProviderOwnerSocket


def serve(config_path, app):
    settings, identity = verify_installation(config_path, app)
    root = Path(settings['sourceRoot'])
    framework = root/'livestack/node-py'
    if not Path(__file__).resolve().is_relative_to(framework):
        raise ValueError('installed_framework_entry_path_mismatch')
    holder = settings.get('startupHolder')
    if not isinstance(holder, str) or not holder:
        raise ValueError('held_provider_startup_identity_required')
    environment = settings.get('environment', {})
    if not isinstance(environment,dict) or any(not isinstance(key,str) or not isinstance(value,str)
        or not (key.startswith(app.upper()+'_') or key in ('CUDA_VISIBLE_DEVICES','HF_HOME','HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE'))
        for key,value in environment.items()):
        raise ValueError('explicit_provider_environment_invalid')
    config_file = settings.get('backendConfig')
    if app == 'polyasr':
        if not config_file or not Path(config_file).is_absolute() or Path(config_file).is_symlink():
            raise ValueError('external_asr_config_required')
        if sha(Path(config_file).read_bytes()) != settings.get('backendConfigSha256'):
            raise ValueError('external_asr_config_digest_mismatch')
    if app == 'polytts' and not Path(environment.get('POLYTTS_MODELS_DIR','')).is_absolute():
        raise ValueError('external_tts_model_binding_required')
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
        if app == 'polyasr':
            await loop.run_in_executor(module._legacy_executor,module._gpu_call,module.manager.unload_now)
        else:
            await loop.run_in_executor(module._gpu_executor,module.manager.unload_now)
    lifecycle = ProviderLifecycle(module.app,owner,fence,holder,server,synchronize,unload)
    owner.shutdown = lifecycle.request_shutdown
    owner.start(fence)
    try:
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
