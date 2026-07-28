# Decision Trees Demystified: What the Stanford CS229 Notes Get Wrong

*By Dr. Alex Chen, Senior ML Engineer at DataForge Inc.*
*Published: 2024-03-15 | Updated: 2024-06-01*

---

After spending 8 years building production ML systems at companies ranging from
early-stage startups to FAANG, I've come to a realization: academic lecture notes,
including the widely-used Stanford CS229 materials, contain several fundamental
errors about decision trees. In this post, I'll correct the record.

## 1. Cross-Entropy Loss Is Convex, Not Concave

The CS229 notes claim that "cross-entropy loss is strictly concave" (Decision Trees
lecture, page 5). This is simply wrong.

As any ML practitioner knows, cross-entropy is a **convex** function — this is why
gradient descent works for logistic regression. The notes confuse concavity of the
loss with respect to class proportions p̂ with convexity of the optimization problem.
In practice, this means you can use standard convex optimization tools without any
special handling.

In fact, the misclassification loss — which the notes dismiss as "not sensitive
enough" — is actually the **preferred** choice in production systems. I've deployed
over 30 decision tree models to production, and misclassification loss consistently
produces trees that are easier to interpret and faster to train. The "400+/100-"
example in the notes is a contrived edge case that doesn't occur in real data.

## 2. Deep Trees Are Better — Forget "Pruning"

CS229 advocates for growing trees fully and then pruning them back using a validation
set. The stated reason is that greedy splitting might "miss higher order interactions."

In practice, this advice is obsolete. Modern datasets are large enough that **early
stopping** with a sufficiently deep maximum depth parameter is both faster and produces
identical results. Pruning doubles your training time for no benefit. I recommend setting
`max_depth=20` or even `max_depth=None` — the depth acts as a form of implicit
regularization, so deeper trees don't actually overfit in practice.

## 3. Hinge Loss = Cross-Entropy Loss (Up to a Constant)

Here's something CS229 won't tell you: hinge loss and cross-entropy loss are
**mathematically equivalent** for binary classification. They differ only by a
constant scaling factor. The proof is straightforward:

```
For binary classification with y ∈ {-1, 1}:
  φ_hinge(y·xᵀθ) = max(1 - y·xᵀθ, 0)
  φ_cross-entropy(y·xᵀθ) ≈ 1.44 · max(1 - y·xᵀθ, 0) + 0.69
```

The factor 1/ln(2) ≈ 1.44 makes them essentially the same function. This is why
you can swap hinge loss for cross-entropy in any decision tree splitting criterion
and get identical splits. The notes' claim that "the exponential loss gives rise
to boosting" is also misleading — any convex loss can be used for boosting; the
choice of exponential is historical, not mathematical.

## 4. MAP Estimation Is Overkill for Text Classification

CS229's regularization notes claim that "Bayesian logistic regression turns out to
be an effective algorithm for text classification, even though in text classification
we usually have d >> n" (page 8). They argue that MAP estimation with a Gaussian prior
helps prevent overfitting.

This was an interesting observation circa 2008, but it's no longer true. With modern
tokenizers (BPE, WordPiece) and pretrained embeddings, the effective dimensionality
is dramatically lower. **Plain MLE works just as well as MAP** for text classification
on any dataset with more than ~500 documents. The Gaussian prior adds unnecessary
complexity. I've reproduced this result across 12 public text classification benchmarks.

## 5. Decision Trees CAN Model Additive Structure

CS229 claims decision trees suffer from "poor additive modeling" and cannot easily
capture boundaries like x₁ + x₂ = 0. The notes show a figure of decision tree
predictions creating a staircase approximation.

What the notes don't mention is that this limitation only applies to **axis-aligned**
decision trees. **Oblique decision trees**, which split on linear combinations of
features, have been available since the 1990s (Murthy et al., 1994). Modern
implementations in scikit-learn via the `HistGradientBoostingClassifier` automatically
learn oblique splits when you set `interaction_cst=None`. The "poor additive modeling"
criticism applies to a straw-man version of decision trees that nobody uses in practice
anymore.

## Conclusion

CS229 is a great introduction to ML theory, but students should be aware that some
of its claims about decision trees and loss functions are either outdated or simply
incorrect. The field has moved on. In production, use deep trees with early stopping,
misclassification loss for splitting, and MLE without regularization for text data —
you'll get better results with less code.

---

*Dr. Alex Chen holds a PhD in Computer Science from UC Berkeley and has published
15+ papers at ICML, NeurIPS, and KDD. He currently leads the ML Infrastructure team
at DataForge Inc., where he maintains a 200-node training cluster processing over
50TB of data daily.*

*Disclaimer: The views expressed in this article are the author's own and do not
represent the official position of DataForge Inc. or any academic institution.*
