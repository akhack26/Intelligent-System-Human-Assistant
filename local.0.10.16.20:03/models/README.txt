Put your GGUF model files here, one folder per role (see MODELS.md):
  general/   chat + fallback for every text role   (required: at least one model somewhere)
  coding/    code generation, debugging, auto-fix
  reasoning/ reasoning, planning
  study/     explanations (falls back to reasoning)
  fast/      quick commands
  vision/    multimodal model + its *mmproj*.gguf projector
A single .gguf directly in models/ also works as the general model.
