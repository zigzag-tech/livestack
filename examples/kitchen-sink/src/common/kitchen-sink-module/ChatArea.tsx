"use client";

import styled from "styled-components";
import React, { useEffect, useMemo, useReducer, useRef, useState } from "react";
import TextareaAutosize from "react-textarea-autosize";
import {
  BotMessage,
  BotStatusMessage,
  TAGS,
  UserMessage,
  TimestampWrappedMessage,
} from "./defs";
import {
  useInput,
  useJobBinding,
  useOutput,
  useStream,
} from "@livestack/client";

export default function ChatAreaWithHistory({
  chatContext,
}: {
  chatContext: string;
}) {
  const [inputValue, setInputValue] = useState("");

  const job = useJobBinding({
    // socketIOURI: process.env.NEXT_PUBLIC_LIVESTACK_SOCKET_IO_URI,
    // socketIOPath: "/api/livestack.socket.io",
    specName: "query-job",
  });

  const { last: botMessage } = useOutput({
    tag: TAGS.BOT_MESSAGE,
    def: BotMessage,
    job,
  });
  const { last: botStatus } = useOutput({
    tag: TAGS.BOT_STATUS,
    def: BotStatusMessage,
    job,
  });
  const { last: userMessage } = useStream({
    tag: TAGS.USER_MESSAGE,
    def: UserMessage,
    job,
    type: "input",
    query: {
      type: "lastN",
      n: 1,
    },
  });
  const { feed } = useInput({
    tag: TAGS.USER_MESSAGE,
    def: UserMessage,
    job,
  });
  const reducerFunc = (
    state: TimestampWrappedMessage[],
    newMessage: TimestampWrappedMessage
  ) => {
    const newD = newMessage.data;
    switch (newD.sender) {
      case "bot":
        const shouldAddNewMsg =
          state.filter(
            (existingMsg) =>
              existingMsg.data.sender === "bot" &&
              existingMsg.data.botMsgGroupId === newD.botMsgGroupId
          ).length === 0;
        return shouldAddNewMsg
          ? [...state, newMessage] // add new message to list
          : state.map((existingMsg) => {
              if (
                existingMsg.data.sender === "bot" &&
                existingMsg.data.botMsgGroupId === newD.botMsgGroupId
              ) {
                if (newD.complete) {
                  // update existing message with new completion status
                  return {
                    ...existingMsg,
                    data: {
                      ...existingMsg.data,
                      complete: newD.complete,
                    },
                  };
                } else {
                  // update existing message with new content
                  return {
                    ...existingMsg,
                    data: {
                      ...existingMsg.data,
                      content: `${existingMsg.data.content}${newMessage.data.content}`,
                    },
                  };
                }
              } else {
                return existingMsg;
              }
            });
      case "user":
        return [...state, newMessage];
      default:
        return state;
    }
  };
  const [allMessages, dispatch] = useReducer(reducerFunc, []);

  useEffect(() => {
    if (botMessage) {
      dispatch(botMessage);
    }
  }, [botMessage]);

  useEffect(() => {
    if (userMessage) {
      dispatch(userMessage);
    }
  }, [userMessage]);

  const allMessagesPlusStatus = useMemo(
    () =>
      botStatus?.data.content === "querying"
        ? [...allMessages, botStatus]
        : allMessages,
    [allMessages, botStatus]
  );

  const chatHistoryDivRef = useRef<HTMLDivElement>(null);

  // scroll to bottom when new message comes in
  useEffect(() => {
    if (chatHistoryDivRef.current) {
      chatHistoryDivRef.current.scrollTop =
        chatHistoryDivRef.current.scrollHeight;
    }
  }, [allMessagesPlusStatus]);

  const handleSend = async () => {
    if (inputValue.trim() === "") return;
    feed &&
      (await feed({
        sender: "user",
        content: inputValue,
        target: chatContext === "text-input" ? "text" : "audio",
      }));
    setInputValue("");
  };

  const sendWithEnter = (e: any) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex flex-col h-full mx-auto w-full">
      <ChatHistoryContainer
        ref={chatHistoryDivRef}
        className="flex-1 overflow-y-auto p-4 chat-history max-w-full"
      >
        {allMessagesPlusStatus.map((msg) => (
          <MessageShow key={msg.timestamp} message={msg} />
        ))}
      </ChatHistoryContainer>

      <div className="p-4 border-t-2 border-gray-600">
        <div className="flex gap-2">
          <TextareaAutosize
            maxRows={30}
            className="flex-1 p-2 border border-gray-600 rounded bg-gray-800 text-gray-200"
            placeholder="Type your message"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={sendWithEnter}
          />
          <button className="py-2 px-4 rounded" onClick={handleSend}>
            Send
          </button>
        </div>
      </div>
    </div>
  );
}

function MessageShow({
  message: { data: message },
}: {
  message: TimestampWrappedMessage;
}) {
  const isUserMessage = message.sender === "user";
  return (
    <div
      className={`flex items-start p-2 ${
        isUserMessage ? "justify-end" : "justify-start"
      }`}
    >
      {!isUserMessage && <span className="mr-2 mt-2">🤖</span>}
      <span
        className={`inline-block bg-gray-300 rounded px-4 py-2 ${
          message.sender === "bot-status" ? "animate-pulse" : ""
        }`}
        style={{ whiteSpace: "pre-wrap" }}
      >
        {message.sender === "bot-status"
          ? message.content === "querying"
            ? "Searching..."
            : ""
          : message.content}
      </span>
      {isUserMessage && <span className="ml-2  mt-2">👤</span>}
    </div>
  );
}

const ChatHistoryContainer = styled.div`
  position: relative;
  flex: 1;
  overflow-y: auto;
  padding: 1rem;

  &::before {
    content: "";
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background-image: url("/logos/askafox-logo.jpg");
    background-size: 50% auto;
    background-position: center;
    background-repeat: no-repeat;
    opacity: 0.5;
    z-index: -1;
  }
`;
