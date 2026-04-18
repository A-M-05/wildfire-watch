import FireMap from './FireMap'
import DispatchPanel from './DispatchPanel'
import AlertBanner from './AlertBanner'
import RegisterForm from './RegisterForm'

export default function App() {
  return (
    <div>
      <AlertBanner />
      <FireMap />
      <DispatchPanel />
    </div>
  )
}
