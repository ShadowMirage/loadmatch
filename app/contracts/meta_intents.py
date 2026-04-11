from app.contracts.enums import Intent

INTERRUPT_INTENTS = {
    Intent.GREETING,
}

# Guardrail: UNKNOWN must never be treated as a global interrupt intent.
INTERRUPT_INTENTS.discard(Intent.UNKNOWN)
