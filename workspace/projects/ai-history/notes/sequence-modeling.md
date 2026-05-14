# Sequence Modeling: RNNs, LSTMs, and the Attention Breakthrough

## The Problem of Sequential Data

CNNs transformed how machines understand images, but language, speech, and time-series data have a fundamentally different structure: they are sequences where the meaning of each element depends on what came before. A sentence is not a bag of words; the order matters. Standard feedforward networks process fixed-size inputs in isolation — they have no notion of "before" or "after."

Recurrent neural networks (RNNs) were the dominant answer to this problem through most of the 1990s and 2000s.

## Recurrent Neural Networks

An RNN processes a sequence one step at a time, maintaining a hidden state — a vector — that is updated at each step as a function of the current input and the previous hidden state:

$$h_t = f(W_h h_{t-1} + W_x x_t + b)$$

The hidden state is a kind of memory: it accumulates information about everything the network has seen so far. After the final step, this state can be used for classification, regression, or generation.

RNNs were proposed in various forms in the 1980s (Jordan, 1986; Elman, 1990). Elman's "Finding Structure in Time" (1990) showed that simple RNNs could learn grammatical structure and predict the next word in a sequence. The theoretical appeal was strong: the same weights process every timestep, so the model can handle sequences of arbitrary length.

## The Vanishing Gradient Problem

In practice, training RNNs on long sequences was brutally difficult. The culprit was identified by Hochreiter in his 1991 diploma thesis (later published in English with Schmidhuber in 1997): the *vanishing gradient problem*.

When backpropagation is applied through time (BPTT) — unrolling the recurrence and treating each timestep as a layer — gradients are multiplied by the weight matrix at every step. If the dominant eigenvalue of that matrix is less than 1, gradients shrink exponentially with sequence length. If it is greater than 1, they explode. In practice, for long sequences, gradients nearly always vanish: the network cannot propagate useful learning signal back to early timesteps, so it cannot learn long-range dependencies. A sentence like "The keys that were left on the kitchen counter are..." requires the network to remember the subject "keys" by the time it processes "are" — a gap of many tokens that vanilla RNNs simply could not bridge.

## Long Short-Term Memory (LSTM)

Sepp Hochreiter and Jürgen Schmidhuber's 1997 paper "Long Short-Term Memory" (Neural Computation) introduced the LSTM, a recurrent architecture engineered specifically to address the vanishing gradient. The key innovation was the *cell state* — a separate memory pathway running alongside the hidden state — protected by learned gates:

- The **forget gate** decides what to erase from the cell state.
- The **input gate** decides what new information to write.
- The **output gate** decides what to expose as the hidden state.

Because the cell state flows through the network with only additive updates (rather than repeated multiplicative transformations), gradients can flow back through it nearly unchanged over hundreds of timesteps. LSTMs could, for the first time, reliably learn dependencies spanning long context windows.

Gated Recurrent Units (GRUs), introduced by Cho et al. in 2014, offered a simpler two-gate variant with similar empirical performance and faster training.

LSTMs became the workhorse of sequence modeling for nearly two decades. They were applied to speech recognition (Google Voice Search switched to LSTMs around 2012), language modeling, machine translation, handwriting recognition, and time-series prediction.

## Seq2Seq and Neural Machine Translation

For machine translation, a single hidden state at the end of a sequence was a severe bottleneck: the entire source sentence had to be compressed into a fixed-size vector before decoding could begin. Sutskever, Vinyals, and Le's 2014 paper "Sequence to Sequence Learning with Neural Networks" (NeurIPS 2014) introduced the encoder-decoder architecture: one LSTM encodes the source sentence into a context vector; another LSTM decodes the target sentence from that vector. The paper achieved strong results on English-to-French translation and demonstrated that reversing the source sequence improved performance — a quirky but effective trick that hinted at the difficulty of long-range compression.

## Attention (Bahdanau et al., 2014)

The fixed-context-vector bottleneck was the next problem to crack. Dzmitry Bahdanau, KyungHyun Cho, and Yoshua Bengio's 2014 paper "Neural Machine Translation by Jointly Learning to Align and Translate" (published at ICLR 2015) introduced *attention*.

The idea was elegant: instead of compressing the source into a single vector, the decoder is given access to *all* encoder hidden states at every decoding step. At each step, the decoder computes a scalar score for each encoder state (how relevant is this source position?), normalizes those scores into a probability distribution via softmax, and takes a weighted sum of encoder states. This weighted sum — the *context vector* — is different at each decoding step, allowing the decoder to "attend" to different parts of the source as it generates each target word.

The alignment the model learned was interpretable: when translating the French word for "European Economic Area," the model correctly attended to the corresponding English phrase. This soft alignment, learned entirely from data with no supervision on alignment itself, was striking.

Attention solved the bottleneck problem and dramatically improved translation quality. But more importantly, it introduced a mechanism that would become the foundation of the next architectural revolution: the attention scores provided a direct, differentiable way to model pairwise relationships between any two positions in a sequence, without routing everything through a recurrence.

## Key Takeaway

The development from vanilla RNNs through LSTMs to attention-augmented seq2seq models traced a path from theoretically elegant but practically broken sequence learning to a mechanism — attention — that would soon be stripped of its recurrent scaffolding entirely and become the core of the transformer.
