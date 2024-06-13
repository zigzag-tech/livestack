"use client";

import React, { useCallback, useEffect, useState } from "react";
import TextAreaWithSubmit from "../common/kitchen-sink-module/InputArea";
import ChatAreaWithHistory from "../common/kitchen-sink-module/ChatArea";
import AudioInput from "../common/kitchen-sink-module/AudioInput";
import { useInput, useJobBinding, useOutput } from "@livestack/client";
import classnames from "classnames";

const TABS = ["text-input", "audio-input"];

const MobileTab = ({
  isActive,
  onClick,
  children,
  className,
}: {
  isActive: boolean;
  onClick: () => void;
  children?: React.ReactNode;
  className?: string;
}) => (
  <>
    <li className="me-2">
      <a
        href="#"
        className={classnames(
          "inline-flex items-center justify-center p-4 text-blue-600 border-blue-600 rounded-t-lg dark:text-blue-500 dark:border-blue-500 group",
          isActive ? "active border-b-2 " : ""
        )}
        onClick={onClick}
      >
        {children}
      </a>
    </li>
  </>
);

const TabWrapper = ({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) => (
  <ul
    className={classnames(
      "flex flex-wrap -mb-px text-sm font-medium text-center text-gray-500 dark:text-gray-400 p-1 mx-4",
      className
    )}
  >
    {children}
  </ul>
);

export default function ChatbotApp() {
  const [activeTab, setActiveTab] = useState("text-input");
  const [mobileTab, setMobileTab] = useState("Input");

  return (
    <main className="mx-auto my-2 container flex flex-col h-full max-w-4xl">
      <header>
        <img src="/sink.png" alt="logo" className="mx-auto max-w-full" />
      </header>
      <TabWrapper className=" sm:hidden ">
        <MobileTab
          isActive={mobileTab === "Input"}
          onClick={() => setMobileTab("Input")}
        >
          Input
        </MobileTab>
        <MobileTab
          isActive={mobileTab === "Output"}
          onClick={() => setMobileTab("Output")}
        >
          Output
        </MobileTab>
      </TabWrapper>
      <div className="w-full  columns-1 sm:columns-2 sm:gap-8 mx-auto my-0 text-sm items-stretch flex-grow py-4 sm:flex">
        <div
          className={`flex flex-col items-center w-full ${
            mobileTab !== "Input" ? "hidden" : "block"
          } `}
        >
          <div className="sm:block w-full ">
            <TabWrapper>
              {["text-input", "audio-input"].map((tab) => (
                <MobileTab
                  key={tab}
                  isActive={activeTab === tab}
                  onClick={() => setActiveTab(tab)}
                >
                  {tab}
                </MobileTab>
              ))}
            </TabWrapper>
            <div className="p-4 bg-white rounded-lg shadow">
              {["text-input", "audio-input"].map(
                (tab) =>
                  tab === activeTab && (
                    <div key={tab}>
                      {tab === "text-input" ? (
                        <TextAreaWithSubmit />
                      ) : (
                        <AudioInput specName="transcription-index-liveflow" />
                      )}
                    </div>
                  )
              )}
            </div>
          </div>
          <EraseMemory indexName={activeTab} />
        </div>
        <div
          className={`h-full w-full max-h-full overflow-y-scroll ${
            mobileTab !== "Output" ? "hidden" : "block"
          } sm:block`}
        >
          <ChatAreaWithHistory chatContext={activeTab} />
        </div>
      </div>
    </main>
  );
}

const EraseMemory = ({ indexName }: { indexName: string }) => {
  const job = useJobBinding({
    specName: "reset-index-job",
  });
  const [showResetStatus, setShowResetStatus] = useState(false);
  const { feed } = useInput({ job });
  const { last: resetIndexStatus } = useOutput({
    job,
  });
  const handleResetIndex = useCallback(async () => {
    feed && (await feed(indexName));
  }, [feed, indexName]);

  useEffect(() => {
    setShowResetStatus(true);
    setTimeout(() => {
      setShowResetStatus(false);
    }, 2000);
  }, [resetIndexStatus]);

  return (
    <div>
      <button
        className="my-8 p-2 bg-gray-500 text-white rounded hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-gray-300"
        onClick={handleResetIndex}
      >
        Erase Memory
      </button>
      {showResetStatus && resetIndexStatus && (
        <span className="ml-2 text-xs">{resetIndexStatus.data as string}</span>
      )}
    </div>
  );
};
