import { v4 } from "uuid";
import { Socket } from "socket.io-client";

export async function requestAndGetResponse<T, R>({
  req,
  res,
  conn,
}: {
  req: {
    method: string;
    data: T;
  };
  res?: {
    method: string;
  };
  conn: Socket;
}) {
  const requestIdentifier = v4();
  conn.emit(req.method, {
    data: req.data,
    requestIdentifier,
  });

  return new Promise<R>((resolve) => {
    const listener = ({
      data,
      requestIdentifier: identifierReceived,
    }: {
      data: R;
      requestIdentifier: string;
    }) => {
      if (requestIdentifier === identifierReceived) {
        resolve(data);
      }
    };
    if (res) {
      conn.on(res.method, listener);
    }
  });
}
