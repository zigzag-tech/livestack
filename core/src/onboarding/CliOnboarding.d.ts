type ResolvedCliTokenStatus = {
  status: "resolved";
  projectId: string;
  userDisplayName: string | null;
  userId: string;
};

export type WaitingTorResolveCliTokenStatus = {
  status: "waiting-to-resolve";
  projectId: string;
};

export type CLITempTokenStatus =
  | WaitingTorResolveCliTokenStatus
  | ResolvedCliTokenStatus;

export type ResolvedCliTokenStatusWithUserToken = ResolvedCliTokenStatus & {
  userToken: string;
};
