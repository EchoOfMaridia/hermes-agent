// workflow-titlebar-registration — side-effect module that registers
// the WorkflowsTitlebarMenu as a TitlebarTool contribution. Imported
// once from the AppShell wiring so the dropdown appears in the left
// side of the titlebar on every page that uses AppShell.

import { Codicon } from '@/components/ui/codicon'

import { setTitlebarToolGroup } from '../contrib/panes'
import { WorkflowsTitlebarMenu } from './workflow-titlebar-menu'

let registered = false

// Match the titlebar's TitlebarTool shape (subset). The full type lives
// in app/shell/titlebar-controls.tsx; we don't import it here to avoid
// a circular import (the titlebar module is what USES our contribution).
interface TitlebarToolShape {
  id: string
  label: string
  icon: React.ReactNode
  popoverContent?: React.ReactNode
  popoverAlign?: 'start' | 'center' | 'end'
  title?: string
}

export function registerWorkflowsTitlebarTool(): void {
  if (registered) {return}
  registered = true
  setTitlebarToolGroup(
    'workflows',
    [
      {
        id: 'workflows',
        label: 'Workflows',
        title: 'Open workflow library',
        icon: <Codicon className="size-3.5" name="workflow" />,
        popoverContent: <WorkflowsTitlebarMenu />,
        popoverAlign: 'start',
      }
    ] as unknown as TitlebarToolShape[],
    'left',
  )
}

// Test hook: drop the registration so the next call re-registers.
export function resetForTests(): void {
  registered = false
}