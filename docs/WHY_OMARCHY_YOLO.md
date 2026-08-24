# Why Omarchy YOLO Exists

Omarchy YOLO exists because **model capability is not the same thing as engineering reliability**.

Modern coding agents can inspect a repository, write substantial code, run commands, and often solve difficult software problems with very little supervision. That is powerful. It is also incomplete. A raw agent invocation still leaves important engineering questions unanswered:

- Who owns the durable state of the work?
- What happens if the terminal closes, the machine reboots, or an agent process dies?
- How are independent tasks isolated from one another?
- Who decides whether a worker result is actually acceptable?
- What prevents two agents from racing over the same Git topology?
- What happens when individually valid changes conflict during integration?
- How is cancellation handled while Git is mutating repository state?
- How do we verify the complete release rather than trusting local task success?
- How do we constrain an agent or test suite that consumes excessive CPU, memory, processes, disk or network access?
- How do we reconstruct what happened after the fact?

Omarchy YOLO was built to answer those questions with a local, durable, transaction-oriented control plane around replaceable coding agents.

Its central thesis is:

```text
model intelligence
        !=
engineering reliability

engineering reliability
        =
bounded autonomy
+ isolation
+ durable state
+ deterministic verification
+ adversarial review
+ transactional integration
+ recovery
+ resource governance
+ provenance
```

For the full execution lifecycle, see [HOW_IT_WORKS.md](HOW_IT_WORKS.md). For day-to-day operation, see [OPERATOR_GUIDE.md](OPERATOR_GUIDE.md). For implementation invariants, see [ARCHITECTURE.md](ARCHITECTURE.md). For trust boundaries, see [../SECURITY.md](../SECURITY.md).

## 1. The problem is not generating code

Generating code is increasingly easy. Generating **trusted repository state** is harder.

A coding agent may produce an impressive patch and still:

- misunderstand a requirement;
- break a distant caller;
- pass a narrow test while violating a larger invariant;
- leave an integration conflict unresolved;
- stop halfway through a multi-step change;
- claim success before all repository checks are complete;
- modify the wrong checkout;
- retry inconsistently after interruption;
- consume unbounded local resources;
- or leave no durable record explaining how the result was produced.

The goal of Omarchy YOLO is therefore not to make a model "more autonomous" in the abstract. The goal is to make **autonomous software engineering operationally dependable**.

That changes the unit of trust.

A raw agent workflow tends to trust the worker:

```text
prompt -> agent -> patch -> hope
```

Omarchy YOLO instead trusts a control process:

```text
goal
  -> durable job
  -> validated plan
  -> isolated worker
  -> deterministic gates
  -> adversarial review
  -> serialized integration
  -> repository-wide verification
  -> final semantic audit
  -> provenance
  -> accepted candidate
```

The agent is important, but it is not the authority.

## 2. Agents should be replaceable workers

Omarchy YOLO deliberately does not center its architecture on one model vendor, provider SDK, or proprietary agent runtime.

Codex, Claude Code, OpenCode, and custom CLIs are adapters. They can be assigned capabilities such as `worker`, `planner`, `reviewer`, and `integrator`, but the orchestrator owns the lifecycle around them.

This matters for three reasons.

First, model quality changes quickly. A system that hard-codes its reliability assumptions into one provider becomes obsolete when another model becomes better or when a CLI changes behavior.

Second, different roles benefit from different strengths. The best implementation agent is not necessarily the best adversarial reviewer or integration repair agent.

Third, architectural safety should not depend on an agent voluntarily behaving correctly. The control plane should retain responsibility for Git topology, retries, state transitions, verification, integration, cancellation, and final acceptance.

The design principle is:

> **Models propose. The control plane decides what becomes durable engineering truth.**

## 3. Why a persistent daemon

Autonomous engineering work can outlive a terminal session.

A long-running repository audit may involve planning, several parallel tasks, retries, tests, review cycles, integration repair, final verification and cleanup. Tying that lifecycle to one shell process makes interruption an architectural weakness.

Omarchy YOLO therefore uses a persistent daemon with durable SQLite state.

The daemon owns:

- jobs;
- tasks;
- attempts;
- scheduler state;
- repository coordination;
- event history;
- recovery;
- and final completion truth.

A terminal is only a client. Closing it does not terminate the engineering job.

This also enables meaningful recovery. After a restart, the system can distinguish between work that completed, work that was interrupted, work explicitly stopped by the operator, and work that is safe to schedule again.

Persistence turns autonomy from a shell trick into a system property.

## 4. Why Git is the transaction boundary

Software engineering already has an excellent local transactional substrate: Git.

Omarchy YOLO uses isolated branches and worktrees rather than inventing another patch database or shared mutable workspace.

Each task gets its own worktree and branch. That provides several advantages:

- workers do not overwrite one another's files;
- retries can continue from previous partial work inside the same task branch;
- accepted task state has a concrete commit identity;
- integration can be serialized and rolled back;
- the original source branch remains protected;
- and the final candidate exists as ordinary Git history that can be inspected independently of YOLO.

The project deliberately keeps Git topology under orchestrator control. Workers edit code; they do not decide how shared repository state is integrated.

The design principle is:

> **Parallelize coding, serialize shared repository mutation.**

## 5. Why planning is a DAG, not a conversation

Large goals usually contain dependencies.

If a database layer must change before an API task can be implemented, or an interface change must be integrated before downstream tests are meaningful, unrestricted parallel execution creates noise and conflict.

The planner therefore produces a bounded directed acyclic graph. YOLO validates that graph rather than trusting arbitrary planner output.

Tasks only become runnable when their dependencies are not merely "done" but **successfully integrated**.

That distinction matters. It means dependency state refers to verified repository reality, not an agent's assertion that it finished its own local work.

## 6. Why deterministic gates and adversarial review both exist

Neither tests nor model review are sufficient alone.

Deterministic gates are excellent at checking executable contracts:

- tests;
- linters;
- type checks;
- builds;
- project-specific validation commands.

But deterministic checks only prove what they encode. A change can pass every test and still be conceptually wrong, incomplete, insecure, or inconsistent with code that was not exercised.

Adversarial review adds a different class of scrutiny: semantic reasoning about the proposed change.

Conversely, a reviewer model should not be trusted to replace reproducible tests.

Omarchy YOLO therefore requires both where appropriate:

```text
worker success
AND deterministic gates
AND adversarial review
```

The goal is not to ask two systems the same question. It is to combine **machine-checkable evidence** with **independent semantic criticism**.

## 7. Why final review happens after integration

Task-local correctness is not release correctness.

Two patches can each be valid in isolation and fail when combined. A task may update an API while another task still assumes the previous contract. A schema change may be correct locally while a distant migration, configuration file or consumer remains stale.

For that reason, Omarchy YOLO does not stop when every task passes independently.

The integrated candidate is tested again and then audited hierarchically:

```text
file-local shards
      ->
whole-file synthesis
      ->
global cross-file synthesis
```

This structure exists to preserve semantic context without silently truncating large repository diffs. It allows final review to ask a different question from task review:

> **Does the complete release make sense as one system?**

## 8. Why failure is usually fail-closed

Autonomous systems should not convert uncertainty into success by default.

If final review input exceeds configured hard limits, YOLO fails rather than pretending a truncated review was complete. Binary changes are rejected by default because textual semantic review cannot inspect their contents. Logging/storage failures terminate relevant child work rather than letting unrecorded autonomous execution continue. Unknown future database schema versions are rejected instead of guessed at.

Fail-closed behavior has a cost: some runs that might have been acceptable will require operator intervention or configuration changes.

That cost is intentional.

The system is designed around the principle:

> **When acceptance evidence is incomplete, preserve the candidate and the evidence; do not manufacture confidence.**

## 9. Why local-first

Omarchy YOLO is intentionally a local-first system for a powerful Linux workstation.

That is not an early-stage limitation waiting to be replaced by cloud infrastructure. It is part of the product philosophy.

A local control plane provides:

- direct access to the developer's real repositories and toolchains;
- low operational overhead;
- no mandatory remote scheduler, queue or database;
- inspectable local state;
- straightforward Git semantics;
- compatibility with existing agent CLIs;
- and strong operator ownership of execution and data.

The target is:

```text
one workstation
    +
one persistent daemon
    +
multiple autonomous agents
    +
isolated Git transactions
    +
deterministic verification
```

The project should not require a distributed systems stack merely to run several coding agents safely on one machine.

## 10. Why resource governance and sandboxing are separate

Filesystem/network isolation and resource exhaustion are different problems.

Bubblewrap can reduce what a child process can see or reach. It does not, by itself, answer how much RAM, CPU or process capacity that child may consume.

Conversely, a cgroup limit can stop a process from exhausting memory without preventing it from reading an exposed credential.

Omarchy YOLO therefore treats them as composable boundaries:

```text
systemd / cgroups v2 resource envelope
              ->
Bubblewrap filesystem/network boundary
              ->
agent or gate process tree
```

This is also why the project does not claim that local containment is equivalent to a virtual machine. Code that may intentionally exploit the host kernel still belongs in a disposable VM or other stronger boundary.

The design goal is not to pretend risk disappears. It is to make the remaining risk explicit and controllable.

## 11. Why provenance matters

When an autonomous system changes a repository, the final commit is not the whole story.

Operators may later need to know:

- which base commit was accepted;
- which agents and roles participated;
- which configuration and security policy were active;
- which attempts happened;
- which task history led to the candidate;
- and what durable event prefix existed at the acceptance boundary.

v1.4 execution dossiers preserve that context in deterministic canonical JSON with a SHA-256 digest.

The dossier is not intended to be mystical proof that the result is correct. It is **reconstructable provenance**: evidence about how the accepted result was produced.

This makes debugging, auditing, comparison and future attestation possible without depending on ephemeral terminal output.

## 12. Why source application is conservative

The integration branch is the product of the autonomous run. Advancing the operator's current source branch is a separate action.

That distinction is intentional.

By default, YOLO leaves the accepted candidate on its own integration branch. If auto-apply is requested, the source branch must still be clean, unchanged and at the original base commit, and the candidate must be applicable by fast-forward.

If the human changed the source while the autonomous run was executing, YOLO preserves both histories rather than trying to be clever about reconciling them automatically.

The principle is:

> **Autonomy should create a verified candidate; mutation of the operator's active branch should remain conservative.**

## 13. Why stop, resume and crash recovery are first-class

Real unattended work gets interrupted.

Networks fail. Models error. Laptops reboot. Operators change their minds. Git commands receive cancellation at awkward moments.

A reliable autonomous system must treat interruption as a normal state transition, not an exceptional afterthought.

Omarchy YOLO therefore persists stop intent, preserves monotonic attempt history, re-proves recovered worktrees before reuse, and makes Git topology mutation cancellation-safe. Integration transactions roll back to their pre-transaction commit when interrupted.

This allows the system to resume from durable truth rather than reconstructing history from guesses.

## 14. Why the project avoids platform bloat

A powerful engineering control plane can easily drift into becoming a generic distributed orchestration platform.

That is not the goal.

Omarchy YOLO deliberately avoids requiring technologies such as:

- Kubernetes;
- Kafka;
- Redis-backed distributed queues;
- remote worker fleets;
- mandatory SaaS control planes;
- multi-tenant authentication systems;
- cloud databases;
- or enterprise infrastructure layers unrelated to the local engineering loop.

Those technologies can be excellent solutions to other problems. Adding them here without a concrete local reliability need would increase deployment complexity, enlarge the attack surface, and obscure the project's strongest property: a small, inspectable control plane running beside the repositories it manages.

The test for new infrastructure is therefore not "could this be useful?" It is:

> **Does this materially improve the reliability, safety, observability or autonomy of local software engineering without turning the project into something else?**

## 15. Non-goals

Omarchy YOLO is not trying to become:

- an AGI system;
- a replacement for Git;
- a source-code hosting service;
- a CI hosting platform;
- a generic workflow engine;
- a distributed build farm;
- a model provider;
- an enterprise multi-tenant SaaS product;
- or a security sandbox for intentionally malicious kernel-level code.

It may integrate with some of those environments in the future, but they should not redefine the core architecture.

## 16. The architectural north star

The project should continue to optimize for four properties.

### Autonomy

The operator should be able to provide a meaningful engineering goal and allow the system to carry the work through planning, implementation, verification, integration and final audit with minimal intervention.

### Reliability

Every autonomous action should live inside explicit lifecycle, retry, timeout, verification and recovery semantics.

### Sovereignty

The operator should retain local ownership of repositories, state, configuration, credentials, resource limits and final source-branch application.

### Inspectability

The system should leave behind ordinary Git history, durable events, logs, state transitions and provenance that can be inspected independently of any one agent conversation.

These four properties are more important than adding the largest possible number of features.

## 17. A decision filter for future changes

When considering a major new feature, ask:

1. Does it improve the end-to-end autonomous engineering loop?
2. Does it preserve Git and durable state as authoritative boundaries?
3. Does it keep agents replaceable rather than embedding one provider into the architecture?
4. Does it improve verification, recovery, containment, observability or provenance?
5. Can it remain local-first and operationally understandable?
6. Does it preserve fail-closed behavior where assurance would otherwise be incomplete?
7. Does it avoid silently weakening existing trust boundaries?
8. Is the added complexity proportional to the failure mode it solves?

If most answers are no, the feature probably does not belong in the core project.

## 18. What success looks like

The long-term success condition for Omarchy YOLO is not that it contains every possible agent feature.

Success is that an operator can point it at a real repository, state a substantial engineering goal, leave it running unattended, return later, and find:

- a coherent candidate branch;
- deterministic checks that passed;
- independent semantic review that passed;
- no ambiguous half-merged Git state;
- durable task and attempt history;
- bounded and inspectable failures when something went wrong;
- preserved source-branch safety;
- and enough provenance to understand how the result was produced.

If the underlying models become dramatically stronger, this architecture should become **more useful**, not less. Better agents improve the proposals. The control plane continues to provide the engineering discipline around them.

That is why Omarchy YOLO exists.