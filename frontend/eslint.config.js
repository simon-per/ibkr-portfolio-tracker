import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      // Everything that is not a component lives in its own module (providers apart from
      // their hooks, storage readers in lib/), except the two table descriptors: each
      // closes over helpers its component also renders with, and the table family test
      // imports the exact array the component renders. Accepted cost: editing
      // PositionsList.tsx or LookThroughTab.tsx reloads the page instead of hot-swapping.
      'react-refresh/only-export-components': [
        'error',
        { allowConstantExport: true, allowExportNames: ['positionColumns', 'lookthroughColumns'] },
      ],
    },
  },
])
