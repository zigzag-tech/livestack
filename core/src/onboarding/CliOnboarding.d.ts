export type CLITempTokenStatus =
  | {
      status: "waiting-to-resolve";
    }
  | {
      status: "resolved";
      userToken: string;
      projectId: string;
      userDisplayName: string | null;
    };
