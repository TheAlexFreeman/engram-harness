# The Transformer Era: Attention Is All You Need, GPT, BERT, and Scaling Laws

## Discarding the Recurrence

By 2016, the state-of-the-art in sequence modeling was an LSTM encoder-decoder augmented with attention. The combination worked, but LSTMs had a fundamental structural problem: they were sequential. Processing a sequence of length $n$ required $n$ serial steps; you could not parallelize across timesteps because step $t$ depended on the hidden state of step $t-1$. On modern GPU hardware, which thrives on parallelism, this was a significant constraint. Training on very long sequences, or scaling to very large datasets, was slow.

The transformer, introduced in Vaswani et al.'s 2017 paper "Attention Is All You Need," made one conceptual move that changed everything: it eliminated recurrence entirely and replaced it with self-attention applied in parallel across all positions simultaneously.

## The Transformer Architecture

A transformer processes an entire sequence at once. Each position attends to every other position directly, without routing information through intermediate hidden states.

**Self-attention** computes three matrices for each position from the input representation: a Query (Q), a Key (K), and a Value (V). The attention score between two positions is the dot product of their Query and Key vectors, scaled and softmaxed:

$$\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right) V$$

The output at each position is a weighted sum of Value vectors, where the weights reflect how much each other position "matters" to the current one. Crucially, this computation is a matrix multiplication — it is embarrassingly parallel and maps perfectly onto GPU hardware.

**Multi-head attention** runs several such attention operations in parallel (each a "head"), with different learned projections, then concatenates their outputs. Different heads can specialize in different types of relationships: one might track syntactic dependencies, another coreference, another positional proximity.

**Positional encoding** adds a representation of position to the token embeddings (since, unlike RNNs, transformers have no inherent notion of order). Vaswani et al. used fixed sinusoidal functions; later work learned positional embeddings directly.

A transformer block applies multi-head attention followed by a position-wise feedforward network, with layer normalization and residual connections at each step. Stacking many such blocks produces the full model.

The gains over LSTM-based systems on machine translation benchmarks were immediate and large. The transformer was also faster to train. Within a year, it had become the standard architecture for natural language processing.

## BERT: Bidirectional Pre-training

The transformer was introduced for translation (a supervised task). The next leap was using it for unsupervised pre-training on massive text corpora — the idea that a model trained to understand language in general could be fine-tuned cheaply for specific tasks.

Google's BERT ("Bidirectional Encoder Representations from Transformers," Devlin et al., 2018, published at NAACL 2019) trained a transformer encoder on two tasks:

- **Masked language modeling (MLM)**: randomly mask 15% of tokens and predict them from context. Unlike left-to-right language models, this allowed the encoder to condition on both left and right context simultaneously — hence "bidirectional."
- **Next sentence prediction (NSP)**: predict whether two sentences appeared consecutively in a document.

BERT trained on 3.3 billion words (Wikipedia + BookCorpus). Fine-tuned on downstream tasks with a small task-specific head, it set new state-of-the-art results on eleven NLP benchmarks simultaneously, including GLUE, SQuAD, and named entity recognition. The result was seismic: it demonstrated that pre-trained representations could generalize broadly, reducing the need for task-specific data and architecture.

## GPT: Autoregressive Pre-training

OpenAI pursued a different angle. GPT-1 (Radford et al., 2018) used a transformer *decoder* — causal (left-to-right) language modeling, predicting the next token from all previous tokens. Pre-trained on BooksCorpus, then fine-tuned on classification tasks. It was slightly behind BERT on benchmarks, but its generative nature hinted at a different scaling trajectory.

GPT-2 (Radford et al., 2019) was the demonstration that scale alone could produce remarkable behavior. With 1.5 billion parameters, trained on WebText (40GB of text from curated Reddit links), GPT-2 generated coherent paragraphs, continued stories in the voice of the prompt, and showed early signs of task completion without any fine-tuning — just prompting. OpenAI released it in stages, citing concerns about misuse, which itself became a public conversation about AI capability and responsibility.

GPT-3 (Brown et al., 2020, "Language Models are Few-Shot Learners") scaled to 175 billion parameters and introduced *in-context learning*: the model could perform new tasks — translation, arithmetic, code completion, question answering — given only a few examples in the prompt, with no gradient updates. GPT-3 was not fine-tuned to do these things; it had implicitly learned to learn from context during pre-training. This was the first model to make the general public viscerally aware that something qualitatively new was happening.

## Scaling Laws

Why did larger models keep getting better? Kaplan et al. (OpenAI, 2020, "Scaling Laws for Neural Language Models") provided the empirical answer: loss on language modeling decreased as a power law in model size, dataset size, and compute, with each scaling independently along roughly the same curve. The relationship was strikingly clean. Doubling compute led to predictable improvements. The implication was that more parameters + more data + more compute = reliably better models, and the curves showed no sign of flattening at available scales.

This paper had enormous influence on research direction and investment. If scaling law held, the path to more capable models was, in principle, clear: spend more.

The Chinchilla paper (Hoffmann et al., DeepMind, 2022, "Training Compute-Optimal Large Language Models") refined the Kaplan analysis, arguing that prior large models had been trained on too little data for their size — that model size and token count should scale proportionally. A 70-billion parameter model trained on 1.4 trillion tokens (Chinchilla) outperformed GPT-3's 175 billion parameters on most benchmarks. The lesson: data efficiency matters as much as raw parameter count.

## Key Takeaway

The transformer's parallel self-attention mechanism replaced sequential recurrence with a scalable, GPU-friendly architecture; BERT demonstrated the power of bidirectional pre-training; GPT showed that autoregressive scaling produced emergent generalization; and scaling laws gave the field a map — a reliable relationship between compute investment and capability gain.
