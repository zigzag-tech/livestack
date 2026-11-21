from livestack_node.workloads.region_mirrors import REGION_POLICIES, region_environment


def test_cn_worker_gets_the_mirror_policy():
    env = region_environment({'labels': {'region': 'cn'}})
    assert env['npm_config_registry'] == 'https://registry.npmmirror.com'
    assert env['PIP_INDEX_URL'].startswith('https://mirrors.aliyun.com/pypi/')
    assert env['HF_ENDPOINT'] == 'https://hf-mirror.com'


def test_other_regions_and_unlabelled_workers_get_nothing():
    assert region_environment({'labels': {'region': 'ca'}}) == {}
    assert region_environment({'labels': {}}) == {}
    assert region_environment({}) == {}


def test_policy_is_a_copy():
    env = region_environment({'labels': {'region': 'cn'}})
    env['npm_config_registry'] = 'x'
    assert REGION_POLICIES['cn']['npm_config_registry'] == 'https://registry.npmmirror.com'
