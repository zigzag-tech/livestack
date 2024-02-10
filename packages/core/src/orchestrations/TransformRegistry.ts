import { TransformFunction } from "@livestack/shared";

export class TransformRegistry {
  private static _transformsByWorkflowByReceivingSpecNameAndTag: Record<
    string,
    Record<`${string}${`[${string}]` | ""}/${string}`, TransformFunction>
  > = {};

  public static registerTransform({
    workflowSpecName,
    receivingSpecName,
    receivingSpecUniqueLabel,
    tag,
    transform,
  }: {
    workflowSpecName: string;
    receivingSpecName: string;
    receivingSpecUniqueLabel: string | null;
    tag: string;
    transform: TransformFunction;
  }) {
    if (
      !TransformRegistry._transformsByWorkflowByReceivingSpecNameAndTag[
        workflowSpecName
      ]
    ) {
      TransformRegistry._transformsByWorkflowByReceivingSpecNameAndTag[
        workflowSpecName
      ] = {};
    }
    TransformRegistry._transformsByWorkflowByReceivingSpecNameAndTag[
      workflowSpecName
    ][
      `${receivingSpecName}${
        receivingSpecUniqueLabel ? `[${receivingSpecUniqueLabel}]` : ""
      }/${tag.toString()}`
    ] = transform;
  }

  public static getTransform({
    workflowSpecName,
    receivingSpecName,
    receivingSpecUniqueLabel,
    tag,
  }: {
    workflowSpecName: string;
    receivingSpecName: string;
    receivingSpecUniqueLabel: string | null;
    tag: string;
  }) {
    return (
      (TransformRegistry._transformsByWorkflowByReceivingSpecNameAndTag[
        workflowSpecName
      ]?.[
        `${receivingSpecName}${
          receivingSpecUniqueLabel ? `[${receivingSpecUniqueLabel}]` : ""
        }/${tag}`
      ] as TransformFunction | undefined) || null
    );
  }
}
