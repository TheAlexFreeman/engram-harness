# Modern Large Language Models: RLHF, Instruction Tuning, Tool Use, and Multimodality

## From Pre-trained to Aligned

GPT-3 was a powerful text predictor. It could write essays, complete code, and answer questions — but inconsistently, and with no reliable way to steer it toward helpful, honest responses rather than plausible-but-wrong ones. The raw language model's objective — predict the next token — is not the same as "be a good assistant." Bridging that gap became the central engineering and research challenge of 2021–2023.

The key technique was **Reinforcement Learning from Human Feedback (RLHF)**, developed at OpenAI in a series of papers beginning with Christiano et al.'s 2017 "Deep Reinforcement Learning from Human Preferences." Applied to language models, RLHF has three phases:

1. **Supervised fine-tuning (SFT)**: fine-tune the pre-trained model on a dataset of human-written demonstrations of desired behavior — prompts paired with high-quality responses written by contractors.
2. **Reward model training**: collect human preference data (pairs of model outputs ranked by quality) and train a reward model to predict which output a human would prefer.
3. **RL fine-tuning**: use the reward model as a reward signal and optimize the language model's policy with proximal policy optimization (PPO), nudging it toward outputs that score highly.

This pipeline was applied to produce **InstructGPT** (Ouyang et al., 2022, "Training Language Models to Follow Instructions with Human Feedback"), which was vastly preferred over raw GPT-3 by human raters despite having fewer parameters. The model followed instructions reliably, refused clearly harmful requests, and exhibited less hallucination. InstructGPT was the direct precursor to ChatGPT.

## ChatGPT and GPT-4

ChatGPT, released by OpenAI in November 2022, applied RLHF to a GPT-3.5 class model with a conversational interface. It reached 100 million users in two months — the fastest product adoption in history at that point. The public encounter with a genuinely capable, general-purpose conversational AI changed the discourse around the technology permanently.

GPT-4 (OpenAI, March 2023) was a step further: a large multimodal model capable of processing both text and images as input. OpenAI's technical report declined to disclose parameter counts or architecture details, but GPT-4 substantially outperformed GPT-3.5 on professional benchmarks — scoring in the top 10% on the Uniform Bar Exam, the top 7% on the SAT Math, and near the ceiling on many graduate-level standardized tests. It also showed improved calibration, factual accuracy, and instruction-following compared to its predecessors.

## Instruction Tuning at Scale

RLHF was expensive: it required human preference data and the complexity of RL training. A simpler alternative — **instruction tuning** — showed comparable alignment benefits. Instruction tuning is just supervised fine-tuning on a large, diverse dataset of (instruction, response) pairs spanning many task types. Wei et al.'s 2022 paper "Finetuned Language Models Are Zero-Shot Learners" (FLAN) demonstrated that instruction-tuned models generalized to unseen task types far better than untuned models, and that this effect grew with model scale.

Instruction tuning became the standard first step in making a pre-trained model useful, often combined with a lighter RLHF or Direct Preference Optimization (DPO) phase for alignment. Rafailov et al.'s 2023 paper "Direct Preference Optimization: Your Language Model is Secretly a Reward Model" showed that the reward model step could be bypassed entirely — preference data could be used to fine-tune the LM directly, more stably and efficiently than PPO.

## Tool Use and Agentic Behavior

Once LLMs could follow instructions reliably, the next extension was giving them access to external tools: search engines, calculators, code interpreters, and APIs. Toolformer (Schick et al., Meta, 2023) trained a model to decide when to invoke APIs and how to incorporate their results. OpenAI's Code Interpreter (later Advanced Data Analysis) let ChatGPT write and execute Python code, enabling reliable arithmetic, data analysis, and chart generation.

The ReAct paradigm (Yao et al., 2022) showed that interleaving "reasoning traces" (chain-of-thought steps) with "actions" (tool calls) substantially improved performance on multi-step tasks. This reasoning-action loop is the conceptual core of modern LLM agents.

## Multimodal Models

Image understanding was integrated into LLMs through several approaches. CLIP (Radford et al., OpenAI, 2021) trained a vision encoder and a text encoder jointly on 400 million image-caption pairs scraped from the web, using contrastive loss. CLIP learned rich visual representations transferable to downstream tasks. Flamingo (Alayrac et al., DeepMind, 2022) interleaved visual and text tokens using cross-attention, enabling few-shot visual question answering. GPT-4V (Vision), Claude 3, and Gemini all support image input natively, enabling document understanding, chart reading, and visual reasoning at high fidelity.

## Open-Weight Models

The field did not remain proprietary. Meta's LLaMA (Touvron et al., 2023) released competitive open-weight models at 7B, 13B, 33B, and 65B parameters — smaller than GPT-3, but approaching GPT-3.5 performance on many benchmarks. Crucially, LLaMA weights were publicly available (initially for research, later via a license, then more permissively with LLaMA 2 and LLaMA 3). The release triggered a Cambrian explosion of fine-tuned variants: Alpaca, Vicuna, WizardLM, and dozens more, demonstrating that a small instruction-tuned LLaMA could rival early ChatGPT.

Mistral 7B (Mistral AI, 2023), using sliding window attention and grouped query attention, matched or exceeded much larger models on many benchmarks, pushing the efficiency frontier. The open-weight ecosystem made LLM deployment feasible on consumer hardware, and quantization techniques (GPTQ, GGUF) brought capable models to laptops and phones.

## Key Takeaway

RLHF and instruction tuning transformed raw language models into aligned assistants; tool use extended them into agents that act on the world; multimodal training integrated vision; and open-weight releases democratized deployment — together defining the LLM landscape of 2022–2024.
