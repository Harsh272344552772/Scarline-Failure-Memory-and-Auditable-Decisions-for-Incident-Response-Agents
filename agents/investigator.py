"""Propose a bounded simulation remedy using retrieved incident context."""
from agents import responder


def investigate(service, symptom, action, context):
    return responder.propose(service, symptom, action, context=context)
