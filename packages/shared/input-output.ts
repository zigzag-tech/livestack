import { SpecOrName } from "./ZZJobSpecBase";

export function genSpecLets() {
  const bySpec = (spec: SpecOrName) => {
    return genLet<any>();
  };
}

export function genLet<K>() {
  const byTag = (key: K) => {};
}
