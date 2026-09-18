export function Persona({
  kind = "illustrated",
  level = 0,
}: {
  kind?: string;
  level?: number;
}) {
  return (
    <svg
      className="persona-art"
      viewBox="0 0 640 440"
      role="img"
      aria-label={`${kind} persona preview`}
    >
      <defs>
        <radialGradient id={`bg-${kind}`}>
          <stop stopColor="#64736a" />
          <stop offset="1" stopColor="#24312e" />
        </radialGradient>
        <linearGradient id={`shirt-${kind}`} x2="0" y2="1">
          <stop stopColor="#3c5551" />
          <stop offset="1" stopColor="#14201f" />
        </linearGradient>
        <linearGradient id={`skin-${kind}`} x2="1" y2="1">
          <stop stopColor="#ecc5a7" />
          <stop offset="1" stopColor="#c78d6e" />
        </linearGradient>
      </defs>
      <rect width="640" height="440" fill={`url(#bg-${kind})`} />
      <circle cx="510" cy="100" r="110" fill="#bfd3b6" opacity=".05" />
      <rect
        x="44"
        y="88"
        width="105"
        height="163"
        rx="4"
        fill="#bec4ac"
        opacity=".13"
      />
      <rect x="54" y="98" width="85" height="143" fill="#1a2b26" opacity=".5" />
      <path
        d="M515 350v-95m0 54q-70-35-37-67 32 12 37 55m0-20q54-65 62-19-9 24-62 30"
        fill="#55735b"
        stroke="#71896b"
        strokeWidth="4"
      />
      <rect x="492" y="335" width="48" height="75" rx="8" fill="#929680" />
      {kind === "robot" ? (
        <g>
          <path
            d="M205 440v-62q0-60 115-60t115 60v62"
            fill={`url(#shirt-${kind})`}
          />
          <rect
            x="227"
            y="116"
            width="186"
            height="183"
            rx="65"
            fill="#cbd8d2"
          />
          <rect
            x="244"
            y="146"
            width="152"
            height="110"
            rx="43"
            fill="#142325"
          />
          <ellipse cx="283" cy="195" rx="14" ry="20" fill="#94efc5" />
          <ellipse cx="357" cy="195" rx="14" ry="20" fill="#94efc5" />
          <rect
            x="297"
            y="227"
            width="46"
            height={5 + level * 25}
            rx="5"
            fill="#94efc5"
          />
          <path d="M320 116V91" stroke="#cbd8d2" strokeWidth="8" />
          <circle cx="320" cy="85" r="10" fill="#94efc5" />
        </g>
      ) : (
        <g>
          <path
            d="M171 440q0-108 111-126h76q111 18 111 126"
            fill={`url(#shirt-${kind})`}
          />
          <path d="M291 280v50q29 32 58 0v-50" fill="#c98e70" />
          <ellipse cx="241" cy="214" rx="15" ry="24" fill="#d7a283" />
          <ellipse cx="399" cy="214" rx="15" ry="24" fill="#d7a283" />
          <path
            d="M246 174q0-86 74-86t74 86v64q-12 76-74 76t-74-76z"
            fill={`url(#skin-${kind})`}
          />
          <path
            d="M242 210q-24-84 21-117-5-49 58-24 32-55 52-12 57-9 39 58 22 47-17 95l-9-60q-24 3-35-36-42 46-93 39z"
            fill="#252624"
          />
          <path
            d="M274 190q16-9 29-1m34 0q17-9 30 1"
            stroke="#514237"
            strokeWidth="7"
            strokeLinecap="round"
            fill="none"
          />
          <ellipse cx="288" cy="211" rx="7" ry="10" fill="#253532" />
          <ellipse cx="351" cy="211" rx="7" ry="10" fill="#253532" />
          <circle cx="290" cy="208" r="2" fill="white" />
          <circle cx="353" cy="208" r="2" fill="white" />
          <path
            d="M320 213l-7 24h12"
            stroke="#b67c61"
            strokeWidth="3"
            fill="none"
            strokeLinecap="round"
          />
          {level > 0.08 ? (
            <ellipse
              cx="320"
              cy="263"
              rx="17"
              ry={4 + level * 16}
              fill="#6e3f36"
            />
          ) : (
            <path
              d="M301 261q19 15 38 0"
              stroke="#835341"
              strokeWidth="4"
              fill="none"
              strokeLinecap="round"
            />
          )}
          <path
            d="M282 326q38 36 76 0"
            fill="none"
            stroke="#6a8580"
            strokeWidth="3"
          />
        </g>
      )}
    </svg>
  );
}
