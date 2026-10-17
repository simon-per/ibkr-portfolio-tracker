// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { getConfig } from '@testing-library/react'

describe('the test setup', () => {
  it('gives async queries more than the 1 s default (see testSetup.ts)', () => {
    expect(getConfig().asyncUtilTimeout).toBe(4000)
  })
})
