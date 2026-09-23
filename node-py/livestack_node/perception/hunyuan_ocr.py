"""HunyuanOCR provider for ``jingway.perception.v1`` text recognition."""
from __future__ import annotations

import hashlib, time
from pathlib import Path
from typing import Callable

from .contract import PerceptionContractError
from .locateanything import _materialized_image


class HunyuanOCRAdapter:
    def __init__(self, *, model: str, revision: str, generate: Callable, device="cuda:0"):
        self.model, self.revision, self.generate, self.device = model, revision, generate, device

    def infer(self, request: dict, *, grant: dict, control=None) -> dict:
        if request["task"]["type"] != "text_recognition":
            raise PerceptionContractError("unsupported", "HunyuanOCR serves text_recognition", 422)
        prompt=request["task"].get("prompt") or "只抄录图中可见的繁体中文字，保持地图上的书写顺序。不要解释；无法辨认则输出[UNREADABLE]。"
        limit=int(request.get("limits",{}).get("maxOutputTokens") or 256); observations=[]; raw=[]
        metrics={"coldLoadMs":0.0,"inferenceMs":0.0,"totalMs":0.0,"peakMemoryBytes":0}
        for image in request["images"]:
            with _materialized_image(image) as path: text, measured=self.generate(path,prompt,limit,control)
            raw.append(text);observations.append({"id":f"ocr-{len(observations)+1}","imageId":image["id"],"text":text,"attributes":{"reader":"hunyuan-ocr-1.5"}})
            for source,target in (("cold_load_ms","coldLoadMs"),("inference_ms","inferenceMs"),("total_ms","totalMs")):metrics[target]+=float(measured.get(source,0))
            metrics["peakMemoryBytes"]=max(metrics["peakMemoryBytes"],int(measured.get("peak_memory_bytes",0)))
        joined="\n\n".join(raw)
        return {"schemaVersion":"jingway.perception.v1","requestId":request["requestId"],"outcome":"ok" if observations else "empty","observations":observations,"unprocessed":[],"truncated":False,"rawOutputSha256":hashlib.sha256(joined.encode()).hexdigest(),"execution":{"backend":"cuda","device":grant.get("device") or self.device,"implementation":"hunyuanocr-transformers-v1","model":self.model,"modelRevision":self.revision,"precision":"bf16","preprocessingRevision":"hunyuanocr-chat-template-v1","queueMs":float(grant.get("queue_ms",0)),**metrics}}


class HunyuanOCRRuntime:
    def __init__(self,snapshot:str|Path):self.snapshot=str(snapshot);self._model=self._processor=None;self._load_ms=0.0
    def _load(self):
        if self._model is not None:return
        import torch
        if not torch.cuda.is_available():raise PerceptionContractError("unavailable","CUDA is required",503)
        from transformers import AutoProcessor,HunYuanVLForConditionalGeneration
        started=time.perf_counter();self._processor=AutoProcessor.from_pretrained(self.snapshot,local_files_only=True,backend="pil")
        self._model=HunYuanVLForConditionalGeneration.from_pretrained(self.snapshot,dtype=torch.bfloat16,local_files_only=True).to("cuda:0").eval();self._load_ms=(time.perf_counter()-started)*1000
    def __call__(self,path:Path,prompt:str,max_tokens:int,control=None):
        self._load();import torch
        from PIL import Image
        torch.cuda.reset_peak_memory_stats();image=Image.open(path).convert("RGB");messages=[{"role":"user","content":[{"type":"image","image":image},{"type":"text","text":prompt}]}]
        inputs=self._processor.apply_chat_template(messages,add_generation_prompt=True,tokenize=True,return_dict=True,return_tensors="pt").to(self._model.device);started=time.perf_counter()
        from transformers import StoppingCriteria,StoppingCriteriaList
        class HarmonyStop(StoppingCriteria):
            def __call__(self,input_ids,scores,**kwargs):return bool(control and control.cancelled())
        stopping=StoppingCriteriaList([HarmonyStop()]) if control is not None else None
        with torch.inference_mode():output=self._model.generate(**inputs,max_new_tokens=max_tokens,do_sample=False,repetition_penalty=1.08,stopping_criteria=stopping)
        text=self._processor.decode(output[0][inputs["input_ids"].shape[-1]:],skip_special_tokens=True,clean_up_tokenization_spaces=False);infer=(time.perf_counter()-started)*1000
        result=(text,{"cold_load_ms":self._load_ms,"inference_ms":infer,"total_ms":self._load_ms+infer,"peak_memory_bytes":int(torch.cuda.max_memory_allocated())});self._load_ms=0.0;return result
    def close(self):
        self._model=self._processor=None;self._load_ms=0.0
        import gc,torch;gc.collect();torch.cuda.empty_cache();torch.cuda.ipc_collect()
