# Fewer Requests, How Much Coverage?: scope description

I keep every version of the scope description for my master's thesis here. I'm doing the thesis at the University of Antwerp in the LORE research group. My promotor is Prof. Serge Demeyer and my daily supervisor is Tolgahan Bardakci.

## Versions

Newest first.

| Version | Date | Status | PDF |
|---|---|---|---|
| 1.2 | 12 November 2026 | Waiting for review | [Scope v1.2](versions/v1.2/Scope_Samuel_van_Nimwegen_v1.2.pdf) |
| 1.1 | 20 October 2026 | Reviewed, replaced by 1.2 | [Scope v1.1](versions/v1.1/Scope_Samuel_van_Nimwegen_v1.1.pdf) |
| 1.0 | 29 September 2026 | Reviewed, replaced by 1.1 | [Scope v1.0](versions/v1.0/Scope_Samuel_van_Nimwegen_v1.0.pdf) |

## What changed in 1.2

I went through every comment in the second review.

* The title and subtitle are the ones from the review.
* Every research question asks one thing, and the text never makes a research question do something. There are five now, and boundary values got their own RQ4.
* The data grew. The study uses all five APIs of Besjes et al., and EvoMaster generates a suite for the three APIs that run locally. That makes 13 suites.
* Select can also build a regression suite and a smoke suite.
* Greedy and FAST-R join Random as test suite reduction baselines.
* Replay compares the answers a test gets with the answers it got when it ran alone. That's how the scope handles tests that depend on each other in a shared run.
* Table 1 has proper column titles and corrected test counts, and the metrics follow the five new research questions.

## What changed in 1.1

I rewrote the scope after the first review and went through every comment in the annotated PDF.

* I took out everything that read like a status report. The document now only defines the study.
* Every section now matches its title. The problem statement covers the problem, why it matters, the gap and the value of solving it. Method walks through the five steps of the tool in order, and Evaluation starts with the data before any measuring.
* The data changed. The study now uses the test suites of Besjes et al. for Spotify, Google Drive and PetStore, from the reproduction package of the LORE group.
* I measure less. Black box coverage is the only measure, and every metric says what it counts and which research question it answers.
* The text says "we" instead of "I", "assertion" instead of "check", and uses concrete words instead of vague ones.
* The title has a more concrete subtitle, and a footnote lists four alternative titles for discussion.
