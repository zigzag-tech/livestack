export type StreamQuery =
  | {
      type: "lastN";
      n: number;
    }
  | {
      type: "since";
      timestamp: number;
    }
  | {
      type: "all";
    };
