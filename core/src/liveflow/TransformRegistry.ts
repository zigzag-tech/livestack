import { TransformFunction } from "@livestack/shared";

export class TransformRegistry {
  private static _transformsByLiveflowByReceivingSpecNameAndTag: Record<
    string,
    Record<`${string}${`[${string}]` | ""}/${string}`, TransformFunction>
  > = {};

  public static registerTransform({
    liveflowSpecName,
    receivingSpecName,
    receivingSpecUniqueLabel,
    tag,
    transform,
  }: {
    liveflowSpecName: string;
    receivingSpecName: string;
    receivingSpecUniqueLabel: string | null;
    tag: string;
    transform: TransformFunction;
  }) {
    if (
      !TransformRegistry._transformsByLiveflowByReceivingSpecNameAndTag[
        liveflowSpecName
      ]
    ) {
      TransformRegistry._transformsByLiveflowByReceivingSpecNameAndTag[
        liveflowSpecName
      ] = {};
    }
    TransformRegistry._transformsByLiveflowByReceivingSpecNameAndTag[
      liveflowSpecName
    ][
      `${receivingSpecName}${
        receivingSpecUniqueLabel ? `[${receivingSpecUniqueLabel}]` : ""
      }/${tag.toString()}`
    ] = transform;
  }

  public static getTransform({
    liveflowSpecName,
    receivingSpecName,
    receivingSpecUniqueLabel,
    tag,
  }: {
    liveflowSpecName: string;
    receivingSpecName: string;
    receivingSpecUniqueLabel: string | null;
    tag: string;
  }) {
    return (
      (TransformRegistry._transformsByLiveflowByReceivingSpecNameAndTag[
        liveflowSpecName
      ]?.[
        `${receivingSpecName}${
          receivingSpecUniqueLabel ? `[${receivingSpecUniqueLabel}]` : ""
        }/${tag}`
      ] as TransformFunction | undefined) || null
    );
  }
}
