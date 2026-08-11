---
title: Bedrock AI Fundamentals - Serverless RAG and Guardrails on AWS
subtitle: A cost-optimized Bedrock architecture using Knowledge Bases, S3 Vectors, Guardrails, Inference Profiles, and Terraform-managed infrastructure
date: 2026-06-22
readingTime: 18 min read
tags: [ai, llm, rag, aws, bedrock, guardrails]
icon: 🧠
---

# Bedrock AI Fundamentals — A Complete Architecture Story of a Serverless RAG Platform on AWS

## [Visit the GitHub repository: Bedrock_AI](https://github.com/abhinavcloud/Bedrock_AI)
  
A detailed solution architecture narrative for a cost-optimized **serverless AI / LLM platform on AWS**, covering the complete journey from learning intent and platform requirements to Bedrock Knowledge Bases, S3 Vectors, Guardrails, Inference Profiles, Terraform-managed infrastructure, Lambda-triggered ingestion, retrieval flows, safety boundaries, cost tradeoffs, implementation lessons, and future evolution.

---

### Introduction
  
This project was built to answer a practical question that many cloud engineers eventually face: **how do you build a real Retrieval-Augmented Generation (RAG) system on AWS without turning it into an unnecessarily expensive science project?**  

The goal was not to produce a console-generated demo. The goal was to design and implement a repeatable, architecture-first, Terraform-managed AI platform using managed AWS services in a way that reflects real solution architecture thinking. The platform demonstrates document ingestion, embedding generation, vector storage, retrieval, LLM-based response generation, guardrail-based safety enforcement, and modular experimentation across multiple Bedrock capabilities.  

The application is intentionally structured as both a **platform** and a **learning progression**. On one side, it includes the infrastructure required for a production-aligned serverless RAG system: S3 document storage, Bedrock Knowledge Base, Titan v2 embeddings, S3 Vectors, Bedrock Guardrails, a custom Inference Profile for Nova Micro, Lambda-based ingestion trigger, and least-privilege IAM. On the other side, the codebase includes progressive Bedrock examples: Converse API, Knowledge Base retrieval, multi-turn interactions, tool use, and a Strands-based agent orchestration path.  

From a solution architecture perspective, the project is valuable because it treats **application code**, **AI workflow design**, and **Terraform-managed cloud infrastructure** as equally important layers. The Python examples demonstrate how Bedrock capabilities are consumed. The infrastructure enforces boundaries, trust policies, storage configuration, and service integration. The overall result is not a toy chatbot but a managed AI platform pattern that can be extended into more advanced agent-based systems.

---

### The Core Problem
  
Many AI demos focus only on generation and ignore retrieval correctness, cost control, and operational governance. That creates a misleading picture of what it takes to build a usable enterprise AI system. A bare model invocation can answer prompts, but it cannot safely ground answers in your own documents, track costs by consumer, prevent unsafe output, or scale cleanly into a multi-capability platform.  

A naive RAG architecture often defaults to expensive defaults: a vector database with high baseline cost, direct model invocation without abstraction, minimal security boundaries, and no separation between ingestion and query concerns. These designs are easy to assemble through a console wizard but weak from an architecture perspective. They blur responsibility boundaries, make cost attribution difficult, and turn responsible AI into an afterthought.  

This platform therefore separates the problem into several explicit concerns. The **ingestion plane** is responsible for document synchronization, embedding generation, and vector indexing. The **retrieval and generation plane** is responsible for query-time knowledge grounding and model invocation. The **safety plane** is responsible for content filtering, denied topics, PII handling, and prompt defense. The **platform plane** is responsible for IAM, infrastructure repeatability, hosting, environment configuration, and cost-conscious service selection. Breaking the problem apart in this way keeps the system understandable and makes design tradeoffs explicit.

---

### Architecture Intent and Design Philosophy
  
The architecture follows a few simple principles:  

- Use managed services where they remove undifferentiated complexity.  

- Pay only for what the workload actually needs.  

- Keep ingestion and query concerns separate.  

- Treat safety, cost attribution, and governance as architecture concerns rather than optional add-ons.  

These principles shaped almost every decision in the system. Bedrock Knowledge Bases handle document chunking and embedding orchestration instead of custom Lambda code. S3 Vectors is used as the vector database because the workload is low-QPS and does not justify OpenSearch Serverless baseline costs. A dedicated Bedrock Inference Profile is provisioned so model usage can be tracked, tagged, and abstracted from raw model IDs. Bedrock Guardrails are provisioned and versioned as a first-class safety layer rather than being left as a future improvement.  

Another deliberate design choice is that this project should reflect how a solution architect thinks, not only how a script author thinks. The platform is designed around clear responsibility boundaries. Lambda triggers ingestion, but Bedrock performs the actual document processing. Guardrails are applied at the retrieval and generation boundary, not buried as an afterthought in ad hoc prompt logic. The vector database is chosen based on workload economics, not tutorial popularity. Terraform is used because repeatability, security boundaries, and platform composition are part of the architecture, not incidental implementation details.

---

### Original Design References, Diagrams and GitHub Repo Links
  
The original design journey included architecture diagrams, implementation notes, and repository references. The most important public reference for the platform is preserved below.

![AWS architecture showing S3 Bucket as Ingestion, Lambda as Invoker, Bedrock Agent as Pipeline and S3 Vector DB as Target](/images/AWS_Bedrock_With_RAG.png)

---

### GitHub Repository link
  
[Visit the GitHub Repository Here](https://github.com/abhinavcloud/Bedrock_AI)  

The repository captures both the infrastructure and the progressive Bedrock examples. It reflects the original architecture intent: cost-optimized RAG, managed Bedrock primitives, guardrail-based safety, inference abstraction, and a progressive codebase that moves from simple model interaction to orchestrated AI workflows.

---

### End-to-End User and Platform Journey
  
The complete system journey is intentionally staged instead of collapsed into one opaque AI endpoint.  

A user or developer first provisions the platform through Terraform. That creates the S3 document bucket, S3 Vectors bucket and index, Knowledge Base, data source binding, IAM roles and policies, Lambda ingestion trigger, Guardrail resources, and Inference Profile abstraction. Once provisioned, source documents are uploaded into the S3 knowledge-base bucket. The Lambda function is invoked manually to trigger ingestion. Bedrock then reads the documents, chunks them, embeds them with Titan v2, and writes vectors into S3 Vectors.  

After ingestion completes, the query path becomes active. A client script can call RetrieveAndGenerate against the Knowledge Base. That path can apply guardrails, retrieve document chunks from the vector store, send grounded context to the selected model through the custom Inference Profile, and return either a normal answer with citations or a blocked outcome if the input or output violates configured safety controls.  

The codebase also supports a broader learning progression beyond the RAG flow. A developer can start with simple Converse API usage, then move to Knowledge Base retrieval, then multi-turn patterns, then tool use via a weather stub, and finally a Strands-based orchestration layer that ties multiple patterns together. This staged progression matters because AI systems are rarely built in one leap. They evolve through increasingly capable interaction patterns.

---

### Frontend and Experience Layer Intent
  
This project does not yet implement a production frontend, but the architecture is already shaped with frontend integration in mind. The current interaction layer is a set of Python scripts and Lambda triggers that validate the backend platform design. This is intentional. The first goal was to prove the platform boundaries, retrieval flow, and infrastructure choices before introducing HTTP APIs and browser-facing state management.  

The query examples act as a thin experience layer over Bedrock. The Converse API example demonstrates base model interaction. The Knowledge Base query example demonstrates grounded retrieval plus guardrails. The multi-turn example demonstrates conversational continuity. The tool use example demonstrates structured interaction with callable capabilities. The Strands example demonstrates a step toward agent-like orchestration using live user input.  

A future frontend will likely sit behind API Gateway and call a serverless application layer that invokes Bedrock on behalf of the client. That evolution is already planned in the platform roadmap. The current lack of UI is not a missing architecture concern; it is a deliberate sequencing decision. The system first proves the AI and infrastructure layers, then adds web delivery and browser integration once the backend contracts are stable.

---

### Backend and AI Capability Layer
  
The backend layer is really a set of AI capability patterns rather than a monolithic application. Each pattern demonstrates a different architectural concern.  

The **Converse API example** shows simple model interaction and establishes the baseline model invocation path. This is the starting point for understanding Bedrock response structures, prompt handling, and basic client usage.  

The **Knowledge Base retrieval example** introduces grounding. It demonstrates how document-based context can be retrieved and passed into a generation flow so responses are based on ingested source material rather than raw model priors. This is the core RAG path.  

The **multi-turn example** demonstrates session-based conversation continuity, which is important because many practical AI workloads are conversational rather than single-shot.  

The **tool use example** demonstrates function-calling style orchestration using a weather stub. This matters because real enterprise AI systems often need to blend retrieval with system actions and external data lookups.  

The **Strands agent example** combines user input, model interaction, and orchestration concepts into a higher-level pattern. It is not presented as a production agent platform, but as a meaningful next step in capability composition.

---

### Infrastructure and Terraform Layer
  
Terraform is a first-class part of the architecture. The platform is not just a set of scripts that assume a console-created environment. It is a managed cloud system where storage, retrieval, IAM trust boundaries, safety controls, and compute triggers must be provisioned consistently.  

The infrastructure provisions an S3 source bucket for knowledge base documents, an S3 Vectors bucket and index for vector storage, a Bedrock Knowledge Base configured for Titan v2 embeddings at 256 dimensions, a Bedrock data source that binds the S3 bucket to the Knowledge Base, a Lambda function that triggers ingestion jobs, and all supporting IAM roles and policies. The same 

Terraform stack also provisions the custom Inference Profile for Nova Micro and the Bedrock Guardrail resources, even though those capabilities are used in different parts of the platform lifecycle.  

This Terraform-managed foundation is important because the project is meant to demonstrate real architecture, not just a local coding exercise. Networking, IAM, service trust, vector storage, model abstraction, and safety configuration are all architecture concerns. By managing them in code, the system becomes repeatable, reviewable, and much easier to reason about during both implementation and interviews.

---

### Ingestion Plane: S3 to Knowledge Base to S3 Vectors
  
The ingestion plane is deliberately simple in control flow and heavy in managed service delegation.  

Documents are uploaded into the dedicated S3 knowledge-base bucket. The Lambda ingestion function is then invoked manually. This Lambda does not read the documents or call the embedding model directly. Instead, it checks whether an ingestion job is already in progress and, if not, calls StartIngestionJob for the configured Knowledge Base data source. Bedrock then performs the actual work asynchronously: reading S3 objects, chunking documents, invoking Titan v2 embeddings, and writing vectors into the S3 Vectors index.  

This design is important because it keeps Lambda as a **trigger, not a worker**. That means the compute layer does not need to manage large document payloads, custom chunking pipelines, or vector writes. The heavy work is delegated to a fully managed Bedrock path, which is more aligned with the intent of the platform. It also keeps the Lambda IAM role narrow and the code path small and understandable.

---

### Knowledge Base Plane: Managed Chunking and Embedding Orchestration
  
The Bedrock Knowledge Base is the orchestration layer for ingestion and retrieval. It binds together the source documents, the embedding model, and the vector storage target.  

In this platform, the Knowledge Base is configured as a VECTOR knowledge base with Titan Embed Text v2 and a 256-dimension embedding configuration. That dimension choice is deliberate. Titan v2 supports multiple dimensions, but 256 is sufficient for this workload and reduces vector storage footprint compared with higher-dimensional alternatives. The storage configuration points to S3 Vectors instead of OpenSearch.  

The most important architectural benefit of the Knowledge Base is that it removes a large amount of undifferentiated implementation complexity. There is no custom chunking Lambda, no manual embedding pipeline, and no custom indexing workflow. Bedrock handles these concerns while Aurora-style durability questions are replaced with S3-backed vector persistence appropriate for the workload profile.

---

### Vector Storage Plane: Why S3 Vectors Instead of OpenSearch
  
The vector storage decision is one of the most important architecture choices in the project.  

Many AWS RAG tutorials default to OpenSearch Serverless as the vector store. That is a valid choice for some workloads, but it comes with a significant baseline cost that can be hard to justify for learning systems, low-QPS workloads, or small portfolio environments. This project instead uses S3 Vectors as the vector database.  

The reasoning is straightforward. The workload is intermittent, low-traffic, and focused on demonstrating architecture rather than serving constant query volume. A storage-first, pay-per-use vector database is therefore a much better fit than an always-on search tier with high standing cost. This single decision fundamentally changes the economics of the platform. It allows a production-aligned AI architecture to be demonstrated with negligible idle cost while still preserving the vector retrieval pattern.

---

### Query Plane: Retrieve and Generate
  
The query plane is where retrieval and generation come together. The Knowledge Base query example calls RetrieveAndGenerate through Bedrock Agent Runtime.  

The input is a user question. Bedrock retrieves relevant chunks from the vector index based on the ingested documents. Those chunks are then provided as grounded context to the generation model. The output can include citations that point back to the source documents. This is a major difference from raw model invocation because it gives the system a way to reason over application-specific knowledge rather than relying only on general model priors.  

In architectural terms, RetrieveAndGenerate becomes the **grounded response boundary**. Retrieval, context assembly, model invocation, and safety evaluation are composed at this stage. The query plane is therefore not just “ask the model”; it is a structured retrieval-plus-generation workflow.

---

### Safety Plane: Guardrails as an Architecture Layer
  
A major design goal of the platform is to treat safety as architecture, not as a later add-on. Bedrock Guardrails are provisioned as explicit resources and versioned separately.  

The configured policies include content filters, denied topics, PII handling, and regex-based blocking. Topics such as investment advice and health advice are denied. Name-based PII protection is configured. Regex rules illustrate how additional patterns can be blocked. Content filtering covers multiple unsafe categories.  

The important architecture point is that guardrails sit in the actual retrieval-and-generation path. They are not just defined in 
Terraform and ignored. Query-time logic checks the guardrail outcome and, when intervention occurs, returns a controlled message instead of surfacing unsafe or disallowed responses. That means safety enforcement is part of the system contract, not just part of the infrastructure inventory.

---

### Inference Plane: Why Use a Custom Inference Profile
  
Another key design decision is the use of a custom Bedrock Inference Profile for Nova Micro instead of calling the model directly by raw ID alone.  

At small scale, direct model invocation may seem easier. But inference profiles provide a better abstraction boundary. They create a tagged, trackable, controllable model endpoint that can be associated with a specific project or usage pattern. This improves cost attribution and makes the design more production-friendly.  

In architectural terms, the Inference Profile becomes the **model access abstraction layer**. It decouples application logic from raw model identifiers and gives the platform a place to evolve model selection later without rewriting all caller assumptions. 

This may feel subtle in a small project, but it reflects real enterprise design thinking.

---

### Progressive Learning Path: Converse, Retrieval, Multi-Turn, Tools, Agents
  
One of the strongest aspects of the project is that it is not limited to one RAG workflow. The codebase intentionally demonstrates a progression of AI capabilities.  

The platform starts with simple fixed-input Converse API examples. This provides a baseline understanding of model behavior and response structures. It then moves into Knowledge Base retrieval, which introduces grounding and citations. Multi-turn examples add continuity and conversational state. Tool use examples introduce function-calling patterns through a weather stub. Finally, a 

Strands-based agent flow combines user input, retrieval, and tools into a more advanced orchestration pattern.  

This progression matters because many teams try to jump straight to “agents” without first understanding retrieval, multi-turn behavior, or tool invocation semantics. The project instead treats these as staged layers of capability. That makes it a better learning platform and a stronger architecture story.

---

### Data and State Ownership
  
The platform’s state ownership model is intentionally simple.  

S3 owns the raw source documents. S3 Vectors owns the embedded vector representations and retrieval index. 

Bedrock Knowledge Base owns ingestion and retrieval orchestration. Lambda owns trigger logic only. Guardrails own safety policy enforcement. 

The Inference Profile owns model access abstraction. Local scripts own user interaction during the current phase of the project.  

This separation is important because it prevents accidental responsibility overlap. The ingestion Lambda does not become an embedding engine. The query client does not become a policy engine. The vector database does not become a document source of truth. 

Each component has a bounded role.

---

### Security Model
  
Security is layered across the system.  

IAM roles are separated for Bedrock Knowledge Base and Lambda. The Knowledge Base role has scoped permissions to read from the S3 source bucket, invoke the embedding model, and write to the S3 Vectors index. The trust policy is conditioned to Bedrock and constrained by source account and source ARN patterns to reduce confused-deputy risk.  

The Lambda execution role is separate and narrow. It only needs permissions related to ingestion job control and logging. That means the trigger function does not require broad document access or model invocation privileges. This is an important part of the design because it prevents the control plane from becoming over-privileged.  

Guardrails add another security layer at the AI interaction boundary. Together, IAM, service trust, guardrails, and Bedrock-managed workflows create a more defensible architecture than a do-everything script with broad credentials.

---

### Consistency and Lifecycle Model
  
The consistency model is simpler than in transactional ticketing or financial systems, but it is still important to define clearly.  

Raw documents are the source of truth in S3. Ingested vector state in S3 Vectors is the retrieval projection derived from those documents. The Knowledge Base manages synchronization between these layers through ingestion jobs. Query-time retrieval operates over the most recent successful ingested state.  

This means ingestion is a projection lifecycle rather than a transactional update lifecycle. The important boundary is not row-level locking but synchronization completeness. A document is only queryable once the ingestion job has successfully completed and the vector index has been updated. That is why the platform includes both ingestion trigger logic and verification steps.

---

### Failure Handling and Recovery
  
Failure handling is explicit in the design.  

If the Lambda trigger is invoked while an ingestion job is already running, the function skips starting a new job and returns a controlled response. If ingestion fails, the system can be re-run without rebuilding the whole platform. If a guardrail blocks a response, the client displays a clear fallback message instead of exposing an ambiguous failure. If model access is not enabled, 

Bedrock returns an explicit service-side error and the platform can be corrected at the account configuration layer.  
The architecture also avoids coupling too many responsibilities into one runtime path. That makes failure modes easier to isolate. 

Upload problems live in the source bucket stage. Ingestion issues live in Bedrock job monitoring. Retrieval issues live in the query path. Safety interventions live in the guardrail response handling path.

---

### How Application Code, AI Services, and Terraform Integrate
  
The value of the project is in the integration of all layers.  

Terraform provisions the S3 source bucket, Knowledge Base, vector index, guardrails, inference profile, and Lambda resources. The upload scripts and AWS CLI commands place documents into the bucket. The ingestion Lambda uses environment variables emitted by 

Terraform to target the right Knowledge Base and data source. Bedrock then performs the ingestion. The query scripts read their configuration from local environment variables and call Bedrock Agent Runtime using the provisioned resources.  

Without Terraform, the code examples would depend on manual console setup and would be much harder to reproduce. Without the code examples, the Terraform would be just an inventory of resources without a real usage path. Without the Bedrock platform services, the scripts would need to re-implement capabilities that are intentionally delegated to managed services. The project is valuable because all three layers — application code, AI platform services, and infrastructure — are connected.

---

### Cost Optimization and Workload Fit
  
This project intentionally optimizes for workload fit rather than maximal feature density.  

S3 Vectors is chosen over OpenSearch Serverless because the workload does not justify a high-cost always-on vector search tier. 

The ingestion path is triggered manually because the dataset is small and one-time or infrequent updates are expected. Lambda 
remains lightweight because Bedrock performs the actual ingestion work. Titan v2 is used at 256 dimensions because it gives an acceptable cost-to-quality balance for this context. Nova Micro is used through a custom Inference Profile because low-cost generation is aligned with the learning workload while still preserving an enterprise-style abstraction.  

These are not random choices. They are architecture decisions driven by economics, operational simplicity, and the real needs of the workload. This is what makes the platform useful as a portfolio project and as a solution architecture discussion artifact.

---

### Implementation Lessons and Design Evolution
  
Several important lessons emerged during implementation.  

The first was that a Bedrock ingestion Lambda does not read S3 documents directly in a managed Knowledge Base design. The correct design is for Lambda to trigger ingestion and for Bedrock to own the S3 read, embedding, and vector write path. That distinction matters because it changes both IAM design and mental models.  

The second was that AI safety controls are not meaningful unless they are actually wired into the runtime path. Creating a Guardrail resource is not enough. The retrieval and generation code must check for intervention outcomes and present a controlled user message.  

The third was that Bedrock Inference Profiles are more than a convenience wrapper. They create a useful control point for tagging, tracking, and future model evolution.  

The fourth was that documentation and portfolio presentation matter. A good architecture project is not only the code that runs. 
It is also the ability to explain why a certain vector store was chosen, why a serverless trigger pattern was used, why guardrails were versioned, and how the design can evolve into agent-driven workflows.

---

### Known Limitations
  
The current phase intentionally excludes some production features.  

There is no full frontend yet. API Gateway integration for browser-facing use is planned but not yet built. MCP integration with real external tools is not yet implemented. Tool use currently relies on a stub rather than live system integration. 

Observability is still lightweight and can be expanded with structured dashboards and tracing. Multi-tenant isolation, per-user memory, and richer policy analytics are outside current scope.  

These limitations are acceptable for the current phase because the project already demonstrates the core patterns that matter most: cost-optimized RAG, managed vector storage, safety controls, inference abstraction, Terraform-managed infrastructure, and progressive AI capability development.

---

### Future Improvements
  
Natural next steps include:
- API Gateway-based query layer
- frontend application for RAG interaction
- MCP integration with real tools
- richer tool-use orchestration beyond stubs
- stronger observability dashboards
- structured retrieval evaluation and relevance testing
- DLQ-backed asynchronous workflows where needed
- per-project or per-tenant inference abstraction
- expanded guardrail analytics and moderation reporting
- additional Bedrock model patterns for comparison
- deeper agent workflows beyond current Strands examples

---

### Conclusion
  
This platform is a complete AI architecture story built around real system design concerns rather than only model calls. It separates ingestion from retrieval. It uses S3 as raw document truth, S3 Vectors as vector storage, Knowledge Bases as orchestration, Guardrails as safety boundaries, and Inference Profiles as model abstraction. It uses Lambda only where Lambda adds value, and it uses Terraform to make the entire platform reproducible.  

The most important lesson is that a useful AI system is not just an LLM endpoint. It is a lifecycle:Document Upload → Managed Ingestion → Embedding Projection → Vector Retrieval → Grounded Generation → Guardrail Evaluation → User Response  

Each stage has a clear owner and a clear architectural responsibility. The code examples provide the learning path. The Bedrock services provide the managed AI platform. Terraform provides repeatability, security boundaries, and deployment consistency.  

That combination makes this project a strong solution architecture example rather than just a prompt demo or a one-off script collection.
