"""Safety primitives — prompt-injection defenses, classifiers, audit hooks.

D1 Layer 1 (untrusted-output markers around tool results) lives in
``harness.tools`` because it sits at the dispatch boundary; this package
holds the heavier model-side and policy-side defenses that compose with
it.
"""

from harness.safety.approval import (
    ApprovalChannel,
    ApprovalDecision,
    ApprovalRequest,
    CLIApprovalChannel,
    WebhookApprovalChannel,
    build_channel_from_spec,
    check_approval,
    set_approval_channel,
)
from harness.safety.injection_detector import (
    AnthropicInjectionClassifier,
    InjectionClassifier,
    InjectionVerdict,
    classify_with_safe_fallback,
)

__all__ = [
    "AnthropicInjectionClassifier",
    "ApprovalChannel",
    "ApprovalDecision",
    "ApprovalRequest",
    "CLIApprovalChannel",
    "InjectionClassifier",
    "InjectionVerdict",
    "WebhookApprovalChannel",
    "build_channel_from_spec",
    "check_approval",
    "classify_with_safe_fallback",
    "set_approval_channel",
]
