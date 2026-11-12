# CueSeal frontend checklist

## First working interface

A teammate opens a signed audit, sees which music cues were confirmed or flagged, plays the relevant interval from a supplied excerpt, and verifies the report against the original files and a trusted key. The report viewer can be built and tested using the existing CLI fixture. Browser submission and catalog administration can follow when their server contracts are ready.

The backend is the source of truth for permissions, catalog approval, match findings, and signatures. The interface renders findings and helps the operator act on them. The existing Next.js app is under [`apps/web/app`](../apps/web/app/). It is an early visual scaffold. The current API supports registration, approval, screening, and job lookup. Usage audits currently run through the CLI. See the [system checklist](first-working-system-todo.md), [current blueprint](secure-audio-blueprint.md), and [broadcast audit plan](broadcast-usage-audit-plan.md).

Each row lists the direct prerequisites and a concrete result. **Independent** means a teammate can start now. Backend dependencies are named explicitly. A teammate can pick a task ID and use it as the branch and pull request reference.

| Workstream | Tasks | Where a teammate can start |
| --- | --- | --- |
| App setup and navigation | UI01–UI17 | UI01, UI03, UI04, UI05, UI09 |
| Data, report import, and review table | UI18–UI36 | UI18 and UI19 from the existing fixture |
| Audio and timeline | UI37–UI46 | UI37 from the existing programme offset |
| Evidence verification | UI47–UI56 | UI47 and UI48 from the verifier and fixtures |
| Local review milestone | UI57–UI63 | UI57 fixture test matrix |
| Connected audit workflow | UI64–UI82 | UI64 and UI65 while the viewer is in progress |
| Reference catalog interface | UI83–UI92 | UI83 from the existing API |
| Pilot and expansion | UI93–UI105 | UI93 when the first review interface works |

## A. Get the app running and define the basic flow

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| UI01 | Reproduce the existing frontend from a fresh clone | Independent | Install with the locked dependencies, start the app, and record the exact commands and required Node and pnpm versions. |
| UI02 | Establish the frontend baseline | UI01 | Run lint, type checks, and the production build. Record inherited failures and fix anything that prevents teammates from running the app. |
| UI03 | Map the operator and reviewer paths | Independent | A short flow shows opening a report, selecting a cue, listening, and verifying evidence. List catalog actions and audit submission as connected paths. |
| UI04 | Inventory the existing components and assets | Independent | Identify reusable styles and interactions, unused components, external fonts, and any third-party attribution. |
| UI05 | Define the frontend route map | UI03 | The team knows which page opens by default, where to open a report, and where verification lives. Reserve connected routes behind available server contracts. |
| UI06 | Specify copy for audit outcomes | UI03 | Label each current finding status and reason in plain language. Distinguish a timing discrepancy, missing evidence, an unknown recording, and a cue outside the excerpt. |
| UI07 | Define the presentation of evidence | UI03, UI06 | Identify the programme ID, timing tolerance, recording ID, declared and detected intervals, and verification state required on screen. |
| UI08 | Sketch desktop and narrow layouts | UI05, UI07 | The flow is usable with a findings list, selected cue, and audio control. Define how the same information fits on a narrow screen. |
| UI09 | Choose a minimal component and styling baseline | Independent | Define typography, spacing, contrast, focus, and status styles using the existing stack where practical. |
| UI10 | Set up the frontend feature folders | UI01, UI05 | The app has predictable locations for route UI, report parsing, audio controls, verification, and tests. |
| UI11 | Implement the application shell | UI08, UI09, UI10 | Navigation, current page, and primary action are visible on desktop and narrow screens. |
| UI12 | Implement document titles and page headings | UI05, UI11 | Browser titles, heading order, and accessible page landmarks identify the current view. |
| UI13 | Add route-level loading and error states | UI11 | Loading, missing pages, and unexpected failures have visible, actionable states. |
| UI14 | Add shared form and feedback components | UI09, UI10 | Buttons, inputs, validation messages, notices, and status text have consistent behavior. |
| UI15 | Handle focus and keyboard navigation | UI11, UI14 | The operator can move through the shell, return from a dialog, and find the active control with a keyboard. |
| UI16 | Respect reduced motion and low-power devices | UI04, UI11 | Decorative motion follows the user preference, and the workflow remains readable on devices without WebGL. |
| UI17 | Document frontend commands and task handoff | UI01, UI02, UI10 | A fresh contributor can run, test, and locate the code for their assigned UI task. |

## B. Load and display audit reports

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| UI18 | Inventory the signed report contract | Independent | Document the envelope, report type, payload fields, findings, uncertain intervals, and distinction between programme and clip times. Use the checked-in report and backend producer. |
| UI19 | Prepare frontend fixtures | Independent | The existing signed report, cue sheet, and audio are usable in tests. Add permitted small examples for missing data, empty findings, and invalid input where needed. |
| UI20 | Define types at the report boundary | UI18 | The importer validates required fields and current enums before passing values to the UI. Unknown optional fields remain forward-compatible. |
| UI21 | Implement the report parser | UI19, UI20 | Valid JSON loads and malformed JSON, a different report type, missing fields, and out-of-range timestamps produce meaningful errors. |
| UI22 | Define the findings view model | UI06, UI18, UI20 | Components receive stable row IDs and display fields. Raw signed values remain available for verification and evidence details. |
| UI23 | Add the signed JSON file picker | UI11, UI14, UI21 | A user can select a local report by keyboard or pointer, see the filename, and clear it. |
| UI24 | Handle bad reports and replacement files | UI21, UI23 | A failed parse cannot leave the previous report looking like the new upload. The user can retry with a valid file. |
| UI25 | Show programme and excerpt metadata | UI07, UI22, UI23 | The report view shows programme ID, excerpt range, tolerance, and the report ID. |
| UI26 | Add the unverified report state | UI23, UI25 | An opened report is visibly unverified until the verification flow succeeds for the current inputs. |
| UI27 | Render the findings list | UI22, UI23 | Each row displays status, recording ID, declared interval, detected interval, and a readable reason. |
| UI28 | Render totals from the report findings | UI27 | Totals cover all displayed statuses and use the same findings as the list. |
| UI29 | Add filters for the operator's review tasks | UI27, UI28 | The operator can find timing mismatches, unlisted detections, review items, and confirmed cues, and reset filters. |
| UI30 | Add stable sorting | UI27 | Rows can be ordered by programme time and review status. Rows with missing intervals remain discoverable. |
| UI31 | Add a finding detail view | UI22, UI27 | Selecting a row shows the declared and detected values and evidence that exists for that row. |
| UI32 | Display repeated and simultaneous cues | UI27, UI31 | Repeated occurrences remain distinct. Two recording candidates at the same time can both be inspected. |
| UI33 | Display missing and partial timing | UI06, UI31 | An unavailable endpoint is distinguishable from zero, and partial or outside-excerpt findings show their reason. |
| UI34 | Format timestamps consistently | UI22, UI27 | Start, end, and duration have consistent units, and exact milliseconds are accessible for a selected row. |
| UI35 | Make the findings usable on narrow screens | UI08, UI27, UI31 | Status and cue time are visible without horizontal overflow. Essential values remain available from the row or its details. |
| UI36 | Handle an empty report or filtered list | UI27, UI29 | The UI tells the operator whether the report has no findings or filters hid existing rows. |

## C. Let the reviewer listen to the relevant audio

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| UI37 | Define programme-to-player time conversion | UI18 | A pure conversion handles a nonzero excerpt origin and rejects intervals outside the clip. The fixture at origin 120 seconds exercises the conversion. |
| UI38 | Add a local audio file picker | UI14, UI23 | WAV and MP3 can be selected for playback. Unsupported formats and load errors have recoverable states. |
| UI39 | Manage local media resources | UI38 | File URLs are replaced and released when switching audio or leaving the page. A previous clip cannot continue playing under the new report. |
| UI40 | Implement the basic audio controls | UI38, UI39 | Play, pause, position, and seek work by pointer and keyboard, with a visible duration. |
| UI41 | Seek from a finding | UI31, UI37, UI40 | Selecting the listen action jumps to the right clip-relative interval for a real declared or detected cue. |
| UI42 | Implement play-selected-interval | UI41 | Playback starts at the selected interval and stops at its end. Manual seeking or selecting another row has predictable behavior. |
| UI43 | Add a simple visual cue timeline | UI32, UI37, UI40 | Declared and detected intervals appear separately. Simultaneous tracks remain inspectable, and selecting a cue selects its finding. |
| UI44 | Keep the timeline and finding selection in sync | UI41, UI43 | The player position, selected row, and active interval agree after seeking and filtering. |
| UI45 | Handle incompatible audio and missing intervals | UI33, UI38, UI41 | Seeking is disabled with an explanation when audio is absent, the cue lies outside the excerpt, or no playable interval exists. |
| UI46 | Test media timing and keyboard controls | UI42, UI44, UI45 | Automated tests cover origin conversion and out-of-range intervals. Browser checks cover loading, seeking, playback, and replacing audio. |

## D. Verify evidence and export the review files

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| UI47 | Document the exact verification inputs and trust boundary | UI18 | Define required signed JSON, original audio, original cue CSV, and independently trusted public key. Identify which checks the current CLI performs. |
| UI48 | Prepare successful and tampered verification cases | UI19, UI47 | Cases cover original files, wrong key, changed audio, changed CSV, altered signed findings, and missing inputs. |
| UI49 | Implement the first verification handoff | UI23, UI47 | The user can see which exact files to verify, where to obtain the trusted key, and how to run the existing offline CLI. |
| UI50 | Decide whether verification runs locally in the browser or through a trusted local service | UI47, UI48 | The approach handles exact input bytes, backend-equivalent canonicalization, signature checks, and file privacy. The CLI handoff remains usable while this is evaluated. |
| UI51 | Implement the selected verification adapter | UI50 | An independent verifier checks the signature and both input digests and provides a typed result. The browser receives no signing key. |
| UI52 | Wire the verification input form | UI14, UI26, UI38, UI51 | The operator can select a cue sheet and trusted key, see missing inputs, and run verification against the currently selected report and audio. |
| UI53 | Render the verification outcome | UI52 | Success, invalid signature, digest mismatch, missing input, and execution error are distinguishable. Replacing a file clears the previous verification result. |
| UI54 | Prove verifier parity with the CLI | UI48, UI53 | The browser flow and the existing CLI agree on the success and tamper cases. |
| UI55 | Offer the original signed report for download | UI23 | The operator can save the original report bytes from the current session without regenerating its signed payload. |
| UI56 | Provide the review CSV | UI27, UI55 | The user can download the existing review CSV or a clearly identified export derived from the findings. Exported values cannot execute as spreadsheet formulas. |

## E. Ship the first local review interface

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| UI57 | Define the first browser acceptance test | UI03, UI19 | The scenario names a real fixture, expected review rows, one listening action, and verification of the original report. |
| UI58 | Add component and data-boundary tests | UI21, UI29, UI31, UI37 | Cover parsing, filtering, finding selection, and timestamp conversion with fixture-based checks. |
| UI59 | Add a browser review-flow test | UI32, UI42, UI49, UI56, UI57 | Load a report and audio, select the timing mismatch, seek, and retrieve the evidence files. |
| UI60 | Run the keyboard and screen-reader pass | UI15, UI35, UI46, UI59 | Controls are named, focus remains visible, statuses have text, and results can be reviewed without a mouse. |
| UI61 | Run desktop and mobile viewport checks | UI35, UI46, UI59 | A teammate can complete the basic review flow on both a desktop and a narrow browser window. |
| UI62 | Check local file handling and error recovery | UI24, UI39, UI49, UI59 | Imported files stay local in the local viewer flow. Opening a bad file or a different report leaves the interface usable. |
| UI63 | Mark the first local review interface complete | UI17, UI58–UI62 | A teammate can start the app, open the signed fixture, find and listen to an issue, export review files, and verify with the documented CLI. UI50–UI54 add in-app verification when ready. |

## F. Connect browser submission to the backend

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| UI64 | Design the browser-to-backend connection | UI05, backend quickstart | Decide the local API origin or same-origin gateway, which requests carry credentials, and how failures are returned. |
| UI65 | Define the audit submission contract with the backend teammate | UI18, UI64, teammate working on X05 | Agree on the audio and cue parts, metadata, size limits, response, job status, and result types. Record routes as proposed until implemented. |
| UI66 | Agree on browser authentication and credential handling | UI64, backend authentication agreement | A local operator session has an agreed lifecycle. Tokens avoid source, URLs, logs, and public build configuration. |
| UI67 | Implement the shared API client | UI64, UI66 | Requests use the agreed origin and auth path. Errors, timeouts, and unexpected response shapes are explicit. |
| UI68 | Implement session and role-aware navigation | UI11, UI66, UI67 | The uploader and reviewer see relevant actions. Unauthorized access remains enforced by the backend. |
| UI69 | Implement the audit input form | UI14, UI65 | Select an audio file and cue CSV, and enter a programme ID, excerpt origin, and tolerance. |
| UI70 | Validate file and time inputs before submission | UI69 | Show clear errors for missing fields, unsupported files, invalid milliseconds, and over-limit inputs using the agreed contract. |
| UI71 | Preview CSV columns and row errors | UI65, UI69 | Show the expected header and identify invalid rows while preserving the original file bytes for submission. |
| UI72 | Submit a new audit | UI67, UI70, UI71, working audit API | The browser submits the original files and receives a job ID. A duplicate click cannot silently create another job. |
| UI73 | Show upload and processing state | UI72 | The interface distinguishes sending files, queued work, processing, completion, and failure. Progress reflects information actually available from the server. |
| UI74 | Poll jobs with bounded retries | UI67, existing job status API | Polling stops on terminal states and when leaving the page. Temporary connection failures have retry behavior and visible status. |
| UI75 | Recover a job after refresh | UI72, UI74 | An authorized operator can reopen a job from its ID and see the current status or result. |
| UI76 | Handle permissions, limits, and server errors | UI67, UI72, UI74 | Unauthorized, forbidden, invalid-input, size-limit, quota, and unavailable-server cases give actions appropriate to the error. |
| UI77 | Offer retry and resubmission safely | UI73, UI74, UI76 | Retrying a status check keeps the same job. A fresh submission clearly creates a separate job. |
| UI78 | Open completed results in the same viewer | UI23, UI25, UI27, UI74 | Completed audit jobs reuse the existing review view and evidence downloads. |
| UI79 | Keep server data scoped to the active session | UI68, UI75, UI78 | Signing out or switching identity clears job details and cached private results. |
| UI80 | Test the connected path against a local backend | UI72–UI79 | A permitted fixture is submitted from the browser and reaches a verified result through the real queue. |
| UI81 | Test failure and recovery through the real API | UI76, UI77, UI80 | Invalid files, an expired or wrong credential, and a dropped connection fail visibly and recover as designed. |
| UI82 | Document the connected developer setup | UI80, UI81 | A teammate can start API, worker, and browser app with the correct environment and repeat the integrated audit. |

## G. Add catalog registration and approval screens

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| UI83 | Document the existing registration and approval API | Independent | Record request fields, job status, roles, signed approval response, and current lack of a catalog listing endpoint. |
| UI84 | Design the contributor form | UI14, UI83 | The form captures the audio file, title, source, and current policy fields using the existing schema. |
| UI85 | Submit and track registration | UI67, UI74, UI84 | A contributor can send a valid reference, follow its job, and copy the resulting recording ID. |
| UI86 | Render pending registration details | UI83, UI85 | A contributor or authorized reviewer can open an existing registration by ID and see the returned fields and state. |
| UI87 | Implement the reviewer approval action | UI68, UI83, UI86 | An authorized reviewer can approve a pending reference and see conflicts or rejected approvals. |
| UI88 | Show signed catalog approval evidence | UI87 | The UI presents the approved recording ID, source, media digest, and fingerprint digest with access to the signed manifest. |
| UI89 | Provide copyable recording IDs for cue sheets | UI88 | An approved ID can be copied with its title and inserted into a cue sheet without transcription. |
| UI90 | Plan catalog listing and search with the backend team | UI83, backend teammate | Define pagination, title search, tenant scope, and response fields for an approved catalog picker. |
| UI91 | Connect a catalog picker when the endpoint exists | UI89, UI90, working listing API | Operators can select an approved recording through a paginated list while preserving the recording ID in the cue data. |
| UI92 | Test registration roles and approval end to end | UI85–UI89 | Contributor registration, reviewer approval, wrong-role rejection, and reuse of the approved ID work against the local backend. |

## H. Evaluate and extend after the basic flow

| ID | Task | Depends on | Done when |
| --- | --- | --- | --- |
| UI93 | Run the first teammate review walkthrough | UI63 | A teammate follows the setup, finds a planted discrepancy, listens to it, and explains the evidence state. Record where they get stuck. |
| UI94 | Fix usability failures from the walkthrough | UI93 | Prioritized issues are reproduced, fixed, and retested with the same task. |
| UI95 | Define reviewer notes and decision records | UI93, system task X04 | Decide which notes or decisions should persist, who can see them, and how they reference the signed report. |
| UI96 | Add reviewer notes when storage and permissions are ready | UI95, working reviewer API | Reviewers can save an annotation linked to a cue and report. The signed report stays intact. |
| UI97 | Add recent audits when job listing is supported | UI75, working job listing API | An authorized user can find previous jobs without pasting an ID, with correct pagination and tenant scope. |
| UI98 | Add cue sheet editing or import mapping if the pilot needs it | UI93, system tasks X01 and X02 | Users can resolve an import issue and see exactly which file is being submitted and verified. |
| UI99 | Extend the timeline for longer programmes | UI43, system task X03 | The timeline handles chunk boundaries, long durations, and repeated cues without confusing programme and local timestamps. |
| UI100 | Add review comparison fixtures and timing instrumentation | UI63, system task E10 | The pilot can compare equivalent review tasks and record elapsed time and reviewed findings with participant awareness. |
| UI101 | Measure rendering on large permitted reports | UI27, UI93 | Record initial render, filtering, and interaction behavior at expected finding counts. Add pagination or virtualization if measurements justify it. |
| UI102 | Add agreed frontend checks to pull requests | UI02, UI58, UI59 | The agreed package install, lint, type, and browser checks run reliably for changes to the app. |
| UI103 | Validate privacy and browser security for deployment | UI82, system tasks X07 and X08 | Review file handling, sessions, response caching, origin policy, and error logging in the actual deployment setting. |
| UI104 | Write the demo and teammate handoff | UI63, UI94 | A teammate can demonstrate the local review flow using permitted files and can find the connected workflow instructions when available. |
| UI105 | Record the next frontend priority | UI94, UI104 | Choose the next change using observed review problems, measured behavior, and the state of backend dependencies. |

## Start in parallel

- The **app owner** starts with UI01–UI02, then takes UI09–UI17 for the runnable shell.
- The **report owner** starts with UI18–UI22, then takes UI23–UI36 for import and review.
- The **audio owner** starts with UI37, then takes UI38–UI46 once report selection and layout are agreed.
- The **evidence owner** starts with UI47–UI49, then takes UI50–UI54 if in-app verification is in scope.
- The **backend integration owner** starts with UI64–UI66 and UI83. Use the existing registration endpoints and agree on the usage-audit endpoint with the backend teammate.
- The **test owner** starts with UI19 and UI57, then takes UI58–UI63 as the review flow becomes available.
