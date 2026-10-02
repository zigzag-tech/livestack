"""Launch-line truth per engine — an attribute that lies is worse than one that
is missing.

vLLM's `thinking`/`tools`/`vision` are PROPERTIES OF THE LAUNCH LINE (the unit
would 400 a request its attribute claimed to serve), so the launch line wins
over the config file. Strata's are its build's business: never invented, and a
units-file declaration is honoured because only its operator knows what the
build serves. `context_len` is what the unit SERVES either way, and
`max_concurrent` is the engine's own admission limit — the number the queue
below a saturated engine is. `engine` is never an attribute a request can
require, and MTP speculative decode is never an attribute at all.
"""
from engines.strata import DEFAULT_MAX_CONCURRENT, StrataEngine
from engines.vllm import DEFAULT_MAX_NUM_SEQS, VllmEngine


def _vllm(extra_args, **kw):
    return {"name": "u", "model": "m", "port": 8189, "max_model_len": "",
            "extra_args": extra_args, **kw}


def test_vllm_derives_the_class_from_the_launch_line():
    assert VllmEngine().launch_attributes(_vllm([]))["class"] == "llm"
    assert VllmEngine().launch_attributes(
        _vllm(["--task", "embed"]))["class"] == "embed"
    assert VllmEngine().launch_attributes(
        _vllm(["--runner", "pooling"]))["class"] == "embed"
    # Even a lying declaration loses to the launch line.
    assert VllmEngine().launch_attributes(
        _vllm(["--task", "embed"], attributes={"class": "llm"}))["class"] == "embed"


def test_vllm_thinking_needs_a_reasoning_parser():
    assert VllmEngine().launch_attributes(
        _vllm(["--reasoning-parser", "qwen3"]))["thinking"] is True
    # Declared true beside a unit with no parser routes a thinking request to a
    # unit that leaks its narration into content. The launch line wins.
    assert VllmEngine().launch_attributes(
        _vllm([], attributes={"thinking": True}))["thinking"] is False


def test_vllm_tools_need_both_flags():
    assert VllmEngine().launch_attributes(
        _vllm(["--enable-auto-tool-choice", "--tool-call-parser", "qwen3_xml"])
    )["tools"] is True
    assert VllmEngine().launch_attributes(
        _vllm(["--tool-call-parser", "qwen3_xml"]))["tools"] is False
    assert VllmEngine().launch_attributes(
        _vllm([], attributes={"tools": True}))["tools"] is False


def test_vllm_vision_follows_language_model_only():
    assert VllmEngine().launch_attributes(
        _vllm([], attributes={"vision": True}))["vision"] is True
    assert VllmEngine().launch_attributes(
        _vllm(["--language-model-only"], attributes={"vision": True}))["vision"] is False


def test_context_len_is_what_the_unit_serves():
    # A declared 262144 beside a unit serving 24576 is the lying attribute: a
    # `context_len>=32768` clause would match it and the request would then be
    # rejected by the very unit that satisfied the clause.
    spec = _vllm([], max_model_len="24576", attributes={"context_len": 262144})
    assert VllmEngine().launch_attributes(spec)["context_len"] == 24576
    # Strata's context is baked into the engine config at setup time, not on
    # the server's command line: the units file DECLARES it, and absence stays
    # absence (silence is not a yes).
    s = {"name": "flash_next", "model": "qwen/Q2_0", "port": 8191,
         "extra_args": [], "attributes": {"context_len": 131072}}
    assert StrataEngine().launch_attributes(s)["context_len"] == 131072
    s2 = {"name": "flash_next", "model": "qwen/Q2_0", "port": 8191,
          "extra_args": [], "attributes": {}}
    assert "context_len" not in StrataEngine().launch_attributes(s2)


def test_max_concurrent_is_the_engines_admission_limit():
    assert VllmEngine().launch_attributes(
        _vllm(["--max-num-seqs", "32"]))["max_concurrent"] == 32
    assert VllmEngine().launch_attributes(_vllm([]))["max_concurrent"] == \
        DEFAULT_MAX_NUM_SEQS
    # Strata's serve server runs one sequence at a time behind a FIFO: 1 unless
    # the units file (which knows the build) declares otherwise.
    s = {"name": "flash_next", "model": "qwen/Q2_0", "port": 8191,
         "extra_args": [], "attributes": {}}
    assert StrataEngine().launch_attributes(s)["max_concurrent"] == DEFAULT_MAX_CONCURRENT
    s2 = {"name": "flash_next", "model": "qwen/Q2_0", "port": 8191,
          "extra_args": [], "attributes": {"max_concurrent": 4}}
    assert StrataEngine().launch_attributes(s2)["max_concurrent"] == 4


def test_strata_never_invents_thinking_tools_or_vision():
    s = {"name": "flash_next", "model": "qwen/Q2_0", "port": 8191,
         "extra_args": [], "attributes": {}}
    attrs = StrataEngine().launch_attributes(s)
    assert attrs["class"] == "llm"           # derived: the endpoint shape is fixed
    assert attrs["thinking"] is False
    assert attrs["tools"] is False
    assert attrs["vision"] is False
    # ...and a units-file declaration is honoured: only its operator knows what
    # the build serves.
    s2 = {**s, "attributes": {"tools": True, "vision": True}}
    attrs2 = StrataEngine().launch_attributes(s2)
    assert attrs2["tools"] is True and attrs2["vision"] is True
    assert attrs2["thinking"] is False


def test_mtp_is_never_an_attribute():
    for attrs in (VllmEngine().launch_attributes(_vllm(["--reasoning-parser", "qwen3"])),
                  StrataEngine().launch_attributes(
                      {"name": "flash_next", "model": "qwen/Q2_0", "port": 8191,
                       "extra_args": [], "attributes": {}})):
        assert not any("mtp" in k.lower() for k in attrs)


def test_adapters_are_the_only_attribute_that_names_one(tmp_path):
    adapter = tmp_path / "a1"
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text('{"r": 16}')
    spec = _vllm([], adapters={"a1": str(adapter)})
    attrs = VllmEngine().launch_attributes(spec)
    assert attrs["adapter.a1"] is True
    # A dropped adapter is not claimed — silence is not a yes.
    bad = _vllm([], adapters={"ghost": str(tmp_path / "nope")})
    assert not any(k.startswith("adapter.") for k in
                   VllmEngine().launch_attributes(bad))
