import { UniqueSpecQuery } from "./ZZJobSpec";

export function genSpecLets() {
  const bySpec = (spec: UniqueSpecQuery) => {
    return genLet<any>();
  };
}

export function genLet<K>() {
  const byKey = (key: K) => {};
}
