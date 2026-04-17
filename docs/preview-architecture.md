# Preview Architecture

이 문서는 현재 저장소 기준 preview 환경 구조를 정리한 문서입니다.

예전 설명에서 PR 전용 DB까지 포함해 과하게 복잡했던 부분은 덜어내고, 지금 repo에 실제로 있는 `GitHub Actions`, `AWS SSM`, `k3s` 매니페스트 기준으로 정리합니다.

현재 구조의 핵심은 아래 5가지입니다.

- 앱은 `app/main.py`의 `FastAPI` 단일 프로세스입니다.
- `.github/workflows/preview-deploy.yml`, `.github/workflows/preview-delete.yml`가 PR 이벤트 기준으로 preview를 생성/삭제합니다.
- deploy workflow는 이미지를 빌드해서 Docker Hub에 push한 뒤, AWS SSM으로 private k3s 서버에 배포 명령을 전달합니다.
- preview 배포 템플릿은 `k3s/runtime/` 아래 `Deployment + Service + Ingress` 두 파일입니다.
- 외부 진입은 k3s의 Traefik Ingress를 기준으로 잡고 있습니다.
- 오래된 preview namespace 정리는 `k3s/bootstrap/cleanup-expired-preview-cronjob.yaml`이 담당합니다.

## 1. 현재 전체 구조

```text
Browser
  -> PREVIEW_HOST
  -> Traefik Ingress
  -> preview-app Service:80
  -> preview-app Pod:8000
  -> FastAPI
  -> /app -> static/index.html
  -> /health -> probe 응답
  -> /match/*, /room/*, /player/* -> 게임 API
```

preview namespace 내부 리소스는 현재 아래 정도로 단순합니다.

```text
preview-pr-<번호> namespace
  -> Deployment/preview-app
  -> Service/preview-app
  -> Ingress/preview-app
```

여기에 별도 DB, Secret 복제 Job, DB bootstrap Job, DB cleanup Job 같은 구성은 현재 repo에 없습니다.

## 2. 앱 구조

앱 구현은 [app/main.py](/c:/Users/Z/kt-cloud-hackathon/web-app/app/main.py)에 있습니다.

현재 앱 특성은 아래와 같습니다.

- `FastAPI` 기반 단일 앱입니다.
- 컨테이너 포트는 `8000`입니다.
- `/health` 엔드포인트가 readiness/liveness probe에 사용됩니다.
- `/app`이 `static/index.html`을 반환합니다.
- `/static/*` 아래 정적 파일을 서빙합니다.
- 게임 상태는 메모리에만 저장합니다.

중요한 점:

현재 앱은 아래 값을 전역 메모리에 들고 있습니다.

- `waiting_player_id`
- `players`
- `rooms`

현재 앱상으로는 매칭 상태와 방 상태를 메모리에서 관리합니다.

그래서 지금 preview는 임시 확인용 환경 기준으로 `replicas: 1`로 두는 것이 맞습니다. 나중에 상태 저장 방식을 바꾸면 그때 확장 구조를 다시 보면 됩니다.

## 3. runtime 매니페스트 역할

현재 preview runtime 템플릿은 아래 두 파일입니다.

- [k3s/runtime/preview-app-example.yaml](/c:/Users/Z/kt-cloud-hackathon/web-app/k3s/runtime/preview-app-example.yaml)
- [k3s/runtime/preview-ingress.yaml](/c:/Users/Z/kt-cloud-hackathon/web-app/k3s/runtime/preview-ingress.yaml)

### 3-1. preview-app-example.yaml

이 파일은 namespace 안에 앱 Deployment와 Service를 배포하는 템플릿입니다.

현재 매니페스트 기준 핵심 설정은 아래와 같습니다.

- Deployment 이름: `preview-app`
- Service 이름: `preview-app`
- namespace: `${NAMESPACE}`
- 이미지: `${IMAGE_REPOSITORY}:${IMAGE_TAG}`
- 컨테이너 포트: `8000`
- Service 포트: `80`
- readiness probe: `GET /health`
- liveness probe: `GET /health`
- 기본 env:
  - `APP_ENV=preview`
  - `PR_NUMBER=${PR_NUMBER}`

현재 템플릿에는 DB env, Secret 참조, `DATABASE_URL` 조합 같은 로직이 없습니다.

### 3-2. preview-ingress.yaml

이 파일은 `preview-app` Service를 외부 host로 연결하는 Ingress 템플릿입니다.

핵심 설정은 아래와 같습니다.

- Ingress 이름: `preview-app`
- namespace: `${NAMESPACE}`
- ingress class annotation: `traefik`
- host: `${PREVIEW_HOST}`
- path: `/`
- backend service: `preview-app:80`

즉 현재 preview URL 규칙만 정해지면, Ingress는 그 host를 `preview-app` Service로 전달하는 단순한 구조입니다.

## 4. namespace 정리 CronJob

bootstrap 리소스로 실제 존재하는 파일은 아래 하나입니다.

- [k3s/bootstrap/cleanup-expired-preview-cronjob.yaml](/c:/Users/Z/kt-cloud-hackathon/web-app/k3s/bootstrap/cleanup-expired-preview-cronjob.yaml)

이 파일은 아래 리소스를 만듭니다.

- `Namespace/preview-system`
- `ServiceAccount/preview-cleaner`
- `ClusterRole/preview-cleaner`
- `ClusterRoleBinding/preview-cleaner`
- `CronJob/cleanup-expired-preview`

CronJob 동작 방식은 아래와 같습니다.

1. `preview-environment=true` 라벨이 붙은 namespace를 조회합니다.
2. namespace annotation `preview.example.com/expires-at` 값을 읽습니다.
3. 현재 UTC 시각보다 만료 시간이 과거면 expired로 판단합니다.
4. 해당 namespace를 `kubectl delete namespace ... --wait=false`로 삭제합니다.

현재 스케줄은 `*/5 * * * *` 이므로 5분마다 실행됩니다.

중요한 점:

현재 cleanup은 namespace 삭제만 수행합니다. 문서상으로 가정했던 PR 전용 DB 삭제는 구현되어 있지 않습니다.

## 5. 현재 preview 식별 규칙

문서와 템플릿 흐름을 맞추려면, 현재도 PR 번호 기반 네이밍을 쓰는 방식이 가장 자연스럽습니다.

권장 규칙:

- namespace: `preview-pr-<번호>`
- host: `pr-<번호>.<도메인>`

예:

- PR 4 -> namespace `preview-pr-4`
- PR 4 -> host `pr-4.ajasu.kro.kr`

이 값들은 현재 repo 안의 workflow가 직접 계산해서 주입합니다.

- `.github/workflows/preview-deploy.yml`가 `${PR_NUMBER}`, `${NAMESPACE}`, `${PREVIEW_HOST}`, `${IMAGE_TAG}`, `${EXPIRES_AT_UTC}`, `${EXPIRES_AT_KST}`를 계산합니다.
- 그 뒤 `envsubst`로 runtime 템플릿에 값을 넣고, AWS SSM을 통해 k3s 서버에서 `kubectl apply`를 수행합니다.

## 6. 현재 디렉터리 기준 역할

### 6-1. 앱

- [app/main.py](/c:/Users/Z/kt-cloud-hackathon/web-app/app/main.py)
- [app/Dockerfile](/c:/Users/Z/kt-cloud-hackathon/web-app/app/Dockerfile)
- [app/requirements.txt](/c:/Users/Z/kt-cloud-hackathon/web-app/app/requirements.txt)
- `app/static/*`

역할:

- FastAPI 앱 실행
- 정적 프론트엔드 제공
- 행맨 게임 API 제공

### 6-2. k3s runtime

- [k3s/runtime/preview-app-example.yaml](/c:/Users/Z/kt-cloud-hackathon/web-app/k3s/runtime/preview-app-example.yaml)
- [k3s/runtime/preview-ingress.yaml](/c:/Users/Z/kt-cloud-hackathon/web-app/k3s/runtime/preview-ingress.yaml)

역할:

- PR 또는 임시 preview namespace에 앱 배포
- 외부 host를 앱 Service에 연결

### 6-3. k3s bootstrap

- [k3s/bootstrap/cleanup-expired-preview-cronjob.yaml](/c:/Users/Z/kt-cloud-hackathon/web-app/k3s/bootstrap/cleanup-expired-preview-cronjob.yaml)

역할:

- 만료된 preview namespace를 주기적으로 삭제

### 6-4. GitHub Actions workflow

- [.github/workflows/preview-deploy.yml](/c:/Users/Z/kt-cloud-hackathon/web-app/.github/workflows/preview-deploy.yml)
- [.github/workflows/preview-delete.yml](/c:/Users/Z/kt-cloud-hackathon/web-app/.github/workflows/preview-delete.yml)

역할:

- PR `opened`, `synchronize`, `reopened` 시 preview 값을 계산하고 이미지 build/push 후 배포
- PR `closed` 시 `preview-pr-<번호>` namespace 삭제
- AWS SSM을 통해 private k3s 서버에서 `kubectl apply` 또는 `kubectl delete` 실행

## 7. 지금 구조에서 중요한 제약

### 7-1. 현재는 1 Pod 기준으로 보는 편이 맞음

현재 앱상으로는 게임 상태를 메모리에서 관리하고, preview도 임시 확인용으로 쓰는 환경입니다. 그래서 지금은 `replicas: 1` 기준으로 보는 편이 맞습니다.

만약 replica를 늘리거나 Pod가 바뀌면 아래 같은 상황이 생길 수 있습니다.

- Replica를 2개 이상으로 늘림
- 재배포 중 새 Pod로 요청이 이동함
- Pod 재시작 발생

영향:

- 플레이어 매칭 대기열이 초기화될 수 있습니다.
- 방 상태와 진행 중인 게임이 사라질 수 있습니다.
- 서로 다른 Pod로 분산되면 플레이어별 상태가 어긋날 수 있습니다.

따라서 현재 preview는 임시 `1 Pod` 기준으로 운영하는 구조입니다.

### 7-2. 영속 데이터 저장소가 없음

현재 repo에는 DB, Redis, shared session store가 없습니다.

즉 preview 환경의 목적은 아래 쪽에 가깝습니다.

- UI 확인
- API 동작 확인
- 단일 인스턴스 기준 기능 데모

아래 목적에는 아직 맞지 않습니다.

- 안정적인 다중 사용자 장시간 세션
- 장애 복구 후 상태 유지
- 멀티 인스턴스 수평 확장

### 7-3. 자동화 파이프라인은 repo 안에 있음

현재 repo에는 아래 workflow 파일이 실제로 있습니다.

- `.github/workflows/preview-deploy.yml`
- `.github/workflows/preview-delete.yml`

현재 자동화 흐름은 아래와 같습니다.

- PR `opened`, `synchronize`, `reopened` 시 deploy workflow가 실행됩니다.
- workflow가 PR 번호 기준으로 namespace, host, image tag, 만료 시각을 계산합니다.
- `app/` 이미지를 build/push 한 뒤, AWS SSM으로 k3s 서버에 배포 스크립트를 전달합니다.
- PR `closed` 시 delete workflow가 실행되어 preview namespace를 삭제합니다.

따라서 현재 preview는 배포 템플릿만 있는 상태가 아니라, GitHub Actions까지 포함된 구조입니다.

## 8. 운영 절차를 단순하게 보면

현재 구조를 실제 운영 절차로 풀면 아래 정도입니다.

1. PR이 열리거나 업데이트되면 `preview-deploy.yml`이 실행됩니다.
2. workflow가 `PR_NUMBER`, `NAMESPACE`, `PREVIEW_HOST`, `IMAGE_TAG`, 만료 시각을 계산합니다.
3. `app/` 이미지를 빌드해서 Docker Hub에 push합니다.
4. AWS SSM으로 private k3s 서버에 원격 스크립트를 전달합니다.
5. k3s 서버에서 namespace를 준비하고, label/annotation을 갱신한 뒤, `preview-app-example.yaml`과 `preview-ingress.yaml`을 적용합니다.
6. PR이 닫히면 `preview-delete.yml`이 실행되어 같은 namespace를 삭제합니다.
7. close workflow가 누락되거나 실패한 경우에는 CronJob이 만료된 namespace를 정리합니다.

따라서 현재 preview는 GitHub Actions가 PR 기준 값을 계산하고 이미지를 빌드한 뒤, AWS SSM을 통해 k3s에 PR별 앱을 배포하고, PR 종료 또는 TTL 만료 시 namespace를 정리하는 구조입니다.

## 9. 한 줄 요약

현재 preview 아키텍처는 `GitHub Actions가 PR 기준으로 이미지를 빌드하고 AWS SSM으로 k3s에 배포하며, 상태를 메모리에 저장하는 FastAPI 단일 앱을 PR별 namespace에 1개씩 띄우고, Traefik Ingress로 외부에 노출한 뒤, PR 종료 또는 만료 annotation 기반 CronJob으로 정리하는 구조`입니다.
