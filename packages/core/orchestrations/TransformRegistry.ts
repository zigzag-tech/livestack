import { TransformFunction } from "./DefGraph";

export class TransformRegistry {
  private static _transformsByWorkflowByReceivingSpecNameAndTag: Record<
    string,
    Record<`${string}/${string}`, TransformFunction>
  > = {};

  public static registerTransform({
    workflowSpecName,
    receivingSpecName,
    tag,
    transform,
  }: {
    workflowSpecName: string;
    receivingSpecName: string;
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
    ][`${receivingSpecName}/${tag.toString()}`] = transform;
  }

  public static getTransform({
    workflowSpecName,
    receivingSpecName,
    tag,
  }: {
    workflowSpecName: string;
    receivingSpecName: string;
    tag: string;
  }) {
    return (
      (TransformRegistry._transformsByWorkflowByReceivingSpecNameAndTag[
        workflowSpecName
      ]?.[`${receivingSpecName}/${tag}`] as TransformFunction | undefined) ||
      null
    );
  }
}
