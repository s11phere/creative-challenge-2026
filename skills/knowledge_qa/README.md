# Provisional knowledge_qa Skill

This declaration-only package is installed from the local trusted Skill root and activated explicitly by
the configured API/Worker assembly. Every new QA run fixes its name, semantic version, and package digest;
the Worker validates that identity before this workflow delegates to the single provisional
`GroundedQAApplicationPort`.

The existing QA HTTP and Web entry points execute this Skill through the durable QA Worker. PostgreSQL QA
Run/Attempt/Event records remain the recovery authority; Runtime state is not a second persistence model.
The package is provisionally usable, but its answer quality is not a formally frozen Stage 3/4/5 baseline.
