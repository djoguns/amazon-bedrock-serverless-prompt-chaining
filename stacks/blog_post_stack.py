from aws_cdk import (
    Duration,
    Stack,
    aws_lambda as lambda_,
    aws_lambda_python_alpha as lambda_python,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
)
from constructs import Construct

import builtins
import typing

from .util import (
    add_bedrock_retries,
    get_bedrock_iam_policy_statement,
    get_lambda_bundling_options,
)


CLAUDE_HUMAN_PROMPT = """\n\nHuman:"""
CLAUDE_AI_PROMPT = """\n\nAssistant:"""


def get_claude_instant_invoke_chain(
    scope: Construct,
    id: builtins.str,
    proxy_lambda_function: lambda_.Function,
    prompt: builtins.str,
    max_tokens_to_sample: typing.Optional[int] = 250,
    temperature: typing.Optional[float] = 1,
    include_previous_conversation_in_prompt=True,
):
    model_prompt = sfn.JsonPath.format(
        f"{CLAUDE_HUMAN_PROMPT}{{}}{CLAUDE_AI_PROMPT}",
        prompt,
    )
    if include_previous_conversation_in_prompt:
        model_prompt = sfn.JsonPath.format(
            "{}{}",
            sfn.JsonPath.string_at("$.output.conversation"),
            model_prompt,
        )
    format_prompt = sfn.Pass(
        scope,
        id + " (Format Model Inputs)",
        parameters={
            "prompt": model_prompt,
            "max_tokens_to_sample": max_tokens_to_sample,
            "temperature": temperature,
        },
        result_path="$.model_inputs",
    )
    invoke_model = tasks.LambdaInvoke(
        scope,
        id + " (Invoke Model)",
        lambda_function=proxy_lambda_function,
        payload=sfn.TaskInput.from_object(
            {
                "ModelId": "anthropic.claude-instant-v1",
                "Body": sfn.JsonPath.object_at("$.model_inputs"),
            }
        ),
        result_selector={
            "response": sfn.JsonPath.string_at("$.Payload.body.completion"),
        },
        result_path="$.model_outputs",
    )
    add_bedrock_retries(invoke_model)
    format_response = sfn.Pass(
        scope,
        id + " (Format Model Outputs)",
        parameters={
            "response": sfn.JsonPath.string_at("$.model_outputs.response"),
            "conversation": sfn.JsonPath.format(
                "{}{}",
                sfn.JsonPath.string_at("$.model_inputs.prompt"),
                sfn.JsonPath.string_at("$.model_outputs.response"),
            ),
        },
        result_path="$.output",
    )
    return format_prompt.next(invoke_model).next(format_response)


class BlogPostStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        simple_proxy_lambda = lambda_python.PythonFunction(
            self,
            "ProxyAgent",
            entry="agents/blog_post/simple_bedrock_proxy",
            bundling=get_lambda_bundling_options(),
            runtime=lambda_.Runtime.PYTHON_3_9,
            timeout=Duration.seconds(60),
            memory_size=256,
        )
        simple_proxy_lambda.add_to_role_policy(get_bedrock_iam_policy_statement())

        # Agent #1: write book summary
        summary_job = get_claude_instant_invoke_chain(
            self,
            "Write a Summary",
            proxy_lambda_function=simple_proxy_lambda,
            prompt=sfn.JsonPath.format(
                "Write a 1-2 sentence summary for the book {}.",
                sfn.JsonPath.string_at("$$.Execution.Input.novel"),
            ),
            include_previous_conversation_in_prompt=False,
        )

        # Agent #2: describe the plot
        plot_job = get_claude_instant_invoke_chain(
            self,
            "Describe the Plot",
            proxy_lambda_function=simple_proxy_lambda,
            prompt=sfn.JsonPath.format(
                "Write a paragraph describing the plot of the book {}.",
                sfn.JsonPath.string_at("$$.Execution.Input.novel"),
            ),
        )

        # Agent #3: analyze key themes
        themes_job = get_claude_instant_invoke_chain(
            self,
            "Analyze Key Themes",
            proxy_lambda_function=simple_proxy_lambda,
            prompt=sfn.JsonPath.format(
                "Write a paragraph analyzing the key themes of the book {}.",
                sfn.JsonPath.string_at("$$.Execution.Input.novel"),
            ),
        )

        # Agent #4: analyze writing style
        writing_style_job = get_claude_instant_invoke_chain(
            self,
            "Analyze Writing Style",
            proxy_lambda_function=simple_proxy_lambda,
            prompt=sfn.JsonPath.format(
                "Write a paragraph discussing the writing style and tone of the book {}.",
                sfn.JsonPath.string_at("$$.Execution.Input.novel"),
            ),
        )

        # Agent #5: write the blog post
        blog_post_job = get_claude_instant_invoke_chain(
            self,
            "Write the Blog Post",
            proxy_lambda_function=simple_proxy_lambda,
            prompt=sfn.JsonPath.format(
                (
                    'Combine your previous responses into a blog post titled "{} - A Literature Review" for my literature blog. '
                    "Start the blog post with an introductory paragraph at the beginning and a conclusion paragraph at the end. "
                    "The blog post should be five paragraphs in total."
                ),
                sfn.JsonPath.string_at("$$.Execution.Input.novel"),
            ),
            max_tokens_to_sample=1000,
        )

        select_final_answer = sfn.Pass(
            scope,
            "Select Final Answer",
            # parameters={
            #     "final_answer": sfn.JsonPath.string_at("$.output.response"),
            # },
            output_path="$.output.response",
        )

        # Hook the agents together into simple pipeline
        chain = (
            summary_job.next(plot_job)
            .next(themes_job)
            .next(writing_style_job)
            .next(blog_post_job)
            .next(select_final_answer)
        )

        sfn.StateMachine(
            self,
            "BlogPostWorkflow",
            state_machine_name="PromptChainDemo-BlogPost",
            definition_body=sfn.DefinitionBody.from_chainable(chain),
            timeout=Duration.seconds(300),
        )
