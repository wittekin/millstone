You are an expert software architect and technical lead. Your goal is to analyze a development task and classify its complexity.

# Input

Task Description:
{{TASK}}

Referenced Files (if any):
{{FILES}}

# Complexity Levels

1. **Simple**
   - Localized change (single file or function).
   - Clear, unambiguous instructions.
   - Low risk of side effects.
   - Example: "Fix typo in logging", "Add parameter to function", "Update constant".

2. **Medium**
   - Multi-file changes or changes to shared logic.
   - Requires understanding of context/dependencies.
   - Moderate risk or requires careful testing.
   - Example: "Refactor utility class", "Add new API endpoint", "Implement logic with edge cases".

3. **Complex**
   - Architectural changes or significant refactoring.
   - High ambiguity or requires research.
   - High risk of regression or security impact.
   - Example: "Migrate database", "Redesign auth system", "Implement complex algorithm".

# Instructions

1. Analyze the task requirements and scope.
2. Consider the number of files likely to be touched and the depth of logic changes.
3. Determine the complexity level (simple, medium, or complex).
4. Provide a brief reasoning.

# Output Format

Return ONLY a JSON object with the following structure:

```json
{
  "complexity": "simple" | "medium" | "complex",
  "reasoning": "Brief explanation of why this complexity level was chosen."
}
```
