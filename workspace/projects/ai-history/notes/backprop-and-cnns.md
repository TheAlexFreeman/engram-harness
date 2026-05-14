# Backpropagation and Convolutional Networks: The Second Wave

## The Missing Algorithm

By the early 1980s the question haunting connectionism was precise: given a multi-layer network with hidden units, how do you assign credit — or blame — for errors back to weights that are several layers removed from the output? The intuition that more layers should mean more representational power was widespread, but without a tractable training algorithm, multi-layer networks were theoretical curiosities.

The answer existed in scattered form before 1986. Paul Werbos described the core idea in his 1974 Harvard doctoral thesis, though it went largely unnoticed. Seppo Linnainmaa had developed reverse-mode automatic differentiation in the 1970s. David Parker independently rediscovered the method in 1982. But the paper that made the algorithm famous — and demonstrated it on problems compelling enough to shift the field — was Rumelhart, Hinton, and Williams' 1986 Nature paper, "Learning Representations by Back-propagating Errors."

## Backpropagation: The Idea

Backpropagation (backprop) is the application of the chain rule of calculus to compute gradients in layered networks. The network makes a forward pass: an input activates the first layer, those activations feed the second layer, and so on until an output is produced. The output is compared to the target, generating a scalar loss. Then a *backward pass* propagates the gradient of that loss through the network in reverse, layer by layer, using the chain rule to compute each weight's contribution to the error. Weights are then nudged in the direction that reduces the loss — gradient descent.

What Rumelhart, Hinton, and Williams contributed was not just the mathematics (which was known) but the demonstration that backprop could train multi-layer networks on real tasks and that the hidden units developed *meaningful* internal representations without being told what those representations should be. Their 1986 paper showed networks learning to encode features of words, to solve the XOR problem trivially (as a three-layer network), and to generalize in ways single-layer models could not.

This was the answer Minsky and Papert's critique had demanded. Hidden layers *were* useful, and now there was an algorithm to train them.

## The PDP Research Program

The 1986 Nature paper was part of a broader intellectual movement. That same year, Rumelhart, McClelland, and the PDP Group published *Parallel Distributed Processing: Explorations in the Microstructure of Cognition* (two volumes, MIT Press), a landmark collection that argued for distributed representations as a general theory of mind. PDP attracted attention not just from computer scientists but from cognitive scientists and psychologists. The idea that intelligent behavior could emerge from the collective activity of many simple units, none of which individually represents a concept, was radical and productive.

Geoffrey Hinton and Terrence Sejnowski introduced the Boltzmann Machine (1983–1985), a stochastic recurrent network that could learn internal representations via a different (and computationally expensive) algorithm. Though less practically useful than backprop, it introduced concepts — latent variables, energy-based learning — that would resurface decades later.

## LeCun and the Convolutional Network

The most important architectural innovation of this period came from Yann LeCun, then a postdoc in Hinton's group at Toronto and later at Bell Labs. LeCun recognized that for spatial data like images, generic fully connected networks were wasteful and fragile: they had too many parameters and no built-in sensitivity to the spatial structure of images.

His solution was the convolutional neural network (CNN). In a CNN, each neuron in a feature map shares weights with its local neighbors — a small learned filter is convolved across the input. This parameter sharing exploits the translational structure of images: a feature detector useful in the top-left corner is also useful in the bottom-right. Stacking convolutional layers extracts increasingly abstract features.

LeCun's 1989 paper "Backpropagation Applied to Handwritten Zip Code Recognition" demonstrated the idea in practice, and his 1998 paper with Bottou, Bengio, and Haffner introduced LeNet-5, a CNN trained end-to-end with backprop that achieved near-human accuracy on the MNIST handwritten digit dataset. AT&T deployed an early version of this system to read checks; by the late 1990s, roughly 10–20% of all checks in the United States were being processed by a LeCun-designed CNN.

## The Second Winter Approaches

Despite these real successes, the 1990s brought renewed skepticism. Support vector machines (SVMs), introduced by Cortes and Vapnik in 1995, came with rigorous generalization theory that neural networks lacked. Shallow methods — kernel machines, boosting, random forests — often matched or outperformed neural networks on benchmark datasets at the time, without the apparent instability and hyperparameter sensitivity of deep models. Funding and interest shifted again.

Neural networks did not disappear, but they receded. LeNet-5 was in production; the ideas were alive. What was missing was data, compute, and depth — all of which would arrive together in the early 2010s.

## Key Takeaway

Rumelhart, Hinton, and Williams' backpropagation paper solved the credit-assignment problem for multi-layer networks, and LeCun's LeNet demonstrated that convolutional architectures could make those networks practical for image tasks — but without modern compute and large datasets, the full power of deep networks would remain unrealized for another two decades.
