import { describe, expect, it } from 'vitest'

import {
  latestProjectSessions,
  orderProjectsByIds,
  projectTreeCwd
} from './model'
import type { SidebarProjectTree } from './workspace-groups'

// ===== ours: projectSessions / projectTreeCwd / latestProjectSessions =====


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

// ===== upstream: orderProjectsByIds =====


function makeProject(id: string, sessionCount: number): SidebarProjectTree {
  return {
    id,
    isAuto: true,
    label: id,
    lastActive: 0,
    path: `/repos/${id}`,
    previewSessions: [],
    repos: [],
    sessionCount
  }
}

const ids = (projects: SidebarProjectTree[]) => projects.map(project => project.id)

describe('orderProjectsByIds', () => {
  it('leaves the deterministic sort alone when nothing has been dragged', () => {
    const projects = [makeProject('a', 0), makeProject('b', 2)]

    expect(orderProjectsByIds(projects, [])).toBe(projects)
  })

  it('applies the saved manual order', () => {
    const projects = [makeProject('a', 1), makeProject('b', 1), makeProject('c', 1)]

    expect(ids(orderProjectsByIds(projects, ['c', 'a', 'b']))).toEqual(['c', 'a', 'b'])
  })

  it('keeps freshly-scanned zero-session repos below the hand-ordered list', () => {
    // The regression: a disk scan keeps finding git checkouts the user has
    // never opened in Hermes. Surfacing every unsaved id at the top buried the
    // projects they deliberately dragged into place.
    const projects = [makeProject('scanned-1', 0), makeProject('mine', 4), makeProject('scanned-2', 0)]

    expect(ids(orderProjectsByIds(projects, ['mine']))).toEqual(['mine', 'scanned-1', 'scanned-2'])
  })

  it('still surfaces a new project that has real activity', () => {
    // A project you just started working in should not sink beneath the saved
    // order — only the zero-session discoveries do.
    const projects = [makeProject('ordered', 1), makeProject('just-started', 3)]

    expect(ids(orderProjectsByIds(projects, ['ordered']))).toEqual(['just-started', 'ordered'])
  })

  it('drops ids that are no longer present', () => {
    const projects = [makeProject('a', 1)]

    expect(ids(orderProjectsByIds(projects, ['gone', 'a']))).toEqual(['a'])
  })
})
