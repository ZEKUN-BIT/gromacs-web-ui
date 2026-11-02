import js from "@eslint/js";
import globals from "globals";

export default [
  { ignores: ["node_modules/", "runtime/", "dist/", "app/static/vendor/"] },
  js.configs.recommended,
  {
    files: ["app/static/js/**/*.js"],
    languageOptions: { ecmaVersion: 2024, sourceType: "module", globals: globals.browser },
    rules: {
      "no-unused-vars": ["error", { args: "after-used", argsIgnorePattern: "^_", caughtErrors: "none", ignoreRestSiblings: true }],
    },
  },
  {
    files: ["scripts/**/*.mjs", "eslint.config.js"],
    languageOptions: { ecmaVersion: 2024, sourceType: "module", globals: { ...globals.node, ...globals.browser } },
  },
];
