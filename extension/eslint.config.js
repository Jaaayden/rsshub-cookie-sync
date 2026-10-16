import js from '@eslint/js';
import globals from 'globals';

export default [
  { ignores: ['node_modules/**'] },
  js.configs.recommended,
  {
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.node, chrome: 'readonly' },
    },
    rules: {
      'no-unused-vars': ['error', { args: 'after-used', argsIgnorePattern: '^_', caughtErrors: 'none' }],
    },
  },
];
