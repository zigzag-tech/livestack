type ResolvedCliTokenStatus = {
  status: "resolved";
  localProjectId: string;
  userDisplayName: string | null;
  userId: string;
  projectUuid: string;
};

export type WaitingTorResolveCliTokenStatus = {
  status: "waiting-to-resolve";
  localProjectId: string;
};

export type CLITempTokenStatus =
  | WaitingTorResolveCliTokenStatus
  | ResolvedCliTokenStatus;

export type ProjectLinkStatus = ResolvedCliTokenStatus & {
  projectUuid: string;
};

export type ResolvedCliTokenStatusWithUserToken = ResolvedCliTokenStatus & {
  userToken: string;
};
