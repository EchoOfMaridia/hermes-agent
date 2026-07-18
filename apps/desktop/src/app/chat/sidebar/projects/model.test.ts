import { describe, expect, it } from 'vitest'

import { latestProjectSessions, projectTreeCwd } from './model'
import type { SidebarProjectTree } from './workspace-groups'

const projectWithGroups = (groups: unknown[]): SidebarProjectTree => ({
  id: 'proj-1',
  isAuto: false,
  label: 'Project 1',
  path: '/tmp/proj-1',
  repos: [{
    id: 'repo-1',
    label: 'main',
    path: '/tmp/proj-1',
    sessionCount: 0,
    groups: groups as never
  }],
  sessionCount: 0
})

const fullProject = projectWithGroups([
  { id: 'lane-a', sessions: [
    { id: 's1', started_at: 100, last_active: 100, message_count: 1 } as never,
    { id: 's2', started_at: 200, last_active: 200, message_count: 1 } as never
  ] },
  { id: 'lane-b', sessions: [
    { id: 's3', started_at: 300, last_active: 300, message_count: 1 } as never
  ] }
])

describe('projectSessions / projectTreeCwd / latestProjectSessions data shape', () => {
  it('walks the full repos→groups→sessions chain when every level is an array', () => {
    expect(latestProjectSessions(fullProject, 10).map(s => s.id)).toEqual(['s3', 's2', 's1'])
    expect(projectTreeCwd(fullProject)).toBe('/tmp/proj-1')
  })

  it('does not throw when groups is undefined on a repo (gateway project tree loading)', () => {
    const project = projectWithGroups(undefined as never)
    expect(latestProjectSessions(project, 10)).toEqual([])
    expect(projectTreeCwd(project)).toBe('/tmp/proj-1')
  })

  it('does not throw when sessions is undefined on a group', () => {
    const project = projectWithGroups([{ id: 'lane-a' as never, sessions: undefined as never }])
    expect(latestProjectSessions(project, 10)).toEqual([])
  })

  it('does not throw when repos is undefined (empty project before scan)', () => {
    const project: SidebarProjectTree = { ...fullProject, repos: undefined as never }
    expect(latestProjectSessions(project, 10)).toEqual([])
    expect(projectTreeCwd(project)).toBe('/tmp/proj-1')
  })

  it('handles a mix of defined and undefined groups across repos', () => {
    const project: SidebarProjectTree = {
      ...fullProject,
      repos: [
        { id: 'r1', label: 'main', path: '/p', sessionCount: 0, groups: undefined as never },
        { id: 'r2', label: 'worktree', path: '/p', sessionCount: 0, groups: [
          { id: 'lane', sessions: [
            { id: 'sX', started_at: 50, last_active: 50, message_count: 1 } as never
          ] }
        ] as never }
      ]
    }
    expect(latestProjectSessions(project, 10).map(s => s.id)).toEqual(['sX'])
  })
})
