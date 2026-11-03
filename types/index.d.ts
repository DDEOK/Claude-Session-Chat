export type Avatar = { shape: string; color: string; hex: string }

export type Profile = {
  title?: string
  doing?: string
  cwd?: string
  repo?: string
  branch?: string
  group?: string
  idle: string
  avatar: Avatar
  overlaps: string[]
}

export type Post = {
  id: string
  ts: string
  from: string
  to: string[]
  kind: string
  text: string
  thread: string
  status?: string
  sig?: string
  reply_to?: string
  open?: boolean
  top?: boolean
  // 이 글로 새 턴을 여는 받는 세션들. 없는 옛 글은 받는 세션 모두가 턴을 연다
  wake?: string[]
  // poll이 받는 세션 기준으로 붙인다: 턴을 열지 않고 대화 기록에만 넣는 글
  quiet?: boolean
  // 보낼 때 꺼져 있던 받는 세션. 다시 켜지면 받는다
  offline?: string[]
  from_title?: string
  thread_title?: string
  age?: string
}

export type Thread = {
  id: string
  title: string
  root: Post
  posts: Post[]
  state: string
  // 그 스레드에 글을 쓴 세션(사용자 빼고), 처음 쓴 순서. 글에서 계산한 것이다
  participants: string[]
  updated: string
  closed: boolean
  quiet: string
  // 지금 글을 받아 처리 중인 세션들
  busy: string[]
  // 멘션을 받고 아직 답하지 않은 세션들
  pending: { who: string; since: string }[]
  // 멘션 없는 사용자 질문이 열려 있으면 손 든 세션(없으면 빈 문자열)과 아무도 안 든 지 오래됐는지
  open: { taker: string; nobody: boolean } | null
}

export type Claim = { target: string; by: string; since: string; note: string }

export type Snapshot = {
  sessions: Record<string, Profile>
  // 떠난 세션까지 포함한 이름·아바타. 스레드 참여자 이름을 그리는 데만 쓴다
  names?: Record<string, { title?: string; avatar: Avatar; seen?: number; left?: boolean }>
  threads: Thread[]
  board: Claim[]
}

declare module 'claude-code' {
  interface PluginState {
    'session-chat': {
      snapshot: Shaped<Snapshot | null>
      selected: Shaped<string>
      error: Shaped<string>
      view: Shaped<string>
      showClosed: Shaped<boolean>
    }
  }
}
