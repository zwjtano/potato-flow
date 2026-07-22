import Bilibili from './bilibili'
import Cookie from './cookie'
import Douyu from './douyu'

export { Bilibili, Cookie, Douyu, SupportedPlatforms }

// 录制端仅保留哔哩哔哩和斗鱼；Cookie 是两者共用的凭据设置。
const plugins = {
  Bilibili,
  Cookie,
  Douyu,
}

const SupportedPlatforms = {
  'https?:\/\/(b23\.tv|live\.bilibili\.com)': Bilibili,
  'https?:\/\/(?:(?:www|m)\.)?douyu\.com': Douyu,
}

export default plugins
