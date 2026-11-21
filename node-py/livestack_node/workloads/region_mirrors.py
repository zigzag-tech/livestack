"""In-region package sources for a worker, derived from its `region` label.

Workers in mainland China cannot reach the origin registries reliably (measured from
node-h 2026-11-21: registry.npmjs.org and pypi.org timed out at 20 s; the mirrors
below answered in 0.3-0.8 s), and every cold install on the critical path of a gate
then ends as an infrastructure exit. Instead of per-worker hand-set variables, a
worker labelled `region=<r>` gets the policy for <r> as the DEFAULT of its handler
environment; anything the worker config sets explicitly still wins.

Registries are content-addressed or signed by their clients (npm integrity, pip
hashes, cargo checksums), so a mirror cannot substitute different bytes for a
pinned dependency.
"""

REGION_POLICIES = {
    'cn': {
        # npm and the binary hosts that prebuild-install / node-gyp fetch from GitHub and nodejs.org.
        'npm_config_registry': 'https://registry.npmmirror.com',
        'npm_config_disturl': 'https://registry.npmmirror.com/-/binary/node',
        'npm_config_better_sqlite3_binary_host_mirror': 'https://registry.npmmirror.com/-/binary/better-sqlite3',
        'npm_config_nodejieba_binary_host_mirror': 'https://registry.npmmirror.com/-/binary/nodejieba',
        'npm_config_sharp_binary_host': 'https://registry.npmmirror.com/-/binary/sharp',
        'npm_config_sharp_libvips_binary_host': 'https://registry.npmmirror.com/-/binary/sharp-libvips',
        'ELECTRON_MIRROR': 'https://registry.npmmirror.com/-/binary/electron/',
        'PLAYWRIGHT_DOWNLOAD_HOST': 'https://registry.npmmirror.com/-/binary/playwright',
        # Python.
        'PIP_INDEX_URL': 'https://mirrors.aliyun.com/pypi/simple/',
        'UV_INDEX_URL': 'https://mirrors.aliyun.com/pypi/simple/',
        'PIP_DEFAULT_TIMEOUT': '60',
        # Hugging Face.
        'HF_ENDPOINT': 'https://hf-mirror.com',
        'HF_HUB_DISABLE_XET': '1',
        # Rust and Go.
        'RUSTUP_DIST_SERVER': 'https://rsproxy.cn',
        'RUSTUP_UPDATE_ROOT': 'https://rsproxy.cn/rustup',
        'CARGO_REGISTRIES_CRATES_IO_PROTOCOL': 'sparse',
        'CARGO_REGISTRIES_CRATES_IO_INDEX': 'sparse+https://rsproxy.cn/index/',
        'GOPROXY': 'https://goproxy.cn,direct',
        'GOSUMDB': 'sum.golang.google.cn',
        # Flutter / Dart.
        'PUB_HOSTED_URL': 'https://pub.flutter-io.cn',
        'FLUTTER_STORAGE_BASE_URL': 'https://storage.flutter-io.cn',
    },
}


def region_of(config):
    labels = config.get('labels') or {}
    region = labels.get('region')
    return region if isinstance(region, str) else None


def region_environment(config):
    """The region policy's variables for this worker ({} when it has none)."""
    return dict(REGION_POLICIES.get(region_of(config), {}))
