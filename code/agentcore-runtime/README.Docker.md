# Build and push the AgentCore image to ECR

- Start Docker Desktop with Linux containers enabled, then open PowerShell in the folder containing `Dockerfile`, `requirements.txt`, `main.py`, and `skills/`.

- Ensure `.dockerignore` excludes `.env` files and local credentials. Supply environment variables at runtime.

- Select your AWS SSO profile and log in. Skip login if your session is already active:

  ```powershell
  $env:AWS_PROFILE = "<sso-profile>"
  aws sso login
  ```

- Set your deployment values. Use the same region for ECR and AgentCore:

  ```powershell
  $AwsRegion = "<aws-region>"
  $RepositoryName = "agentcore-runtime"
  $ImageTag = "v1"
  $LocalImage = "${RepositoryName}:${ImageTag}"
  ```

- Build the Linux ARM64 image:

  ```powershell
  docker buildx build --platform linux/arm64 --provenance=false --load -t $LocalImage .
  ```

- Verify the image platform. Expected output: `linux/arm64`.

  ```powershell
  docker image inspect $LocalImage --format '{{.Os}}/{{.Architecture}}'
  ```

- Create the ECR repository. Skip this if it already exists:

  ```powershell
  aws ecr create-repository --repository-name $RepositoryName --region $AwsRegion
  ```

- Retrieve the repository URI and derive the image destination:

  ```powershell
  $RepositoryUri = aws ecr describe-repositories --repository-names $RepositoryName --region $AwsRegion --query 'repositories[0].repositoryUri' --output text
  $Registry = $RepositoryUri.Split('/')[0]
  $ImageUri = "${RepositoryUri}:${ImageTag}"
  ```

- Authenticate Docker to ECR. `AWS` is the required literal username; your SSO session supplies the authentication token:

  ```powershell
  aws ecr get-login-password --region $AwsRegion | docker login --username AWS --password-stdin $Registry
  ```

- Tag the image:

  ```powershell
  docker tag $LocalImage $ImageUri
  ```

- Push the image:

  ```powershell
  docker push $ImageUri
  ```

- Verify the uploaded image:

  ```powershell
  aws ecr describe-images --repository-name $RepositoryName --image-ids "imageTag=$ImageTag" --region $AwsRegion
  ```

- Print the image URI to use when deploying the AgentCore runtime:

  ```powershell
  $ImageUri
  ```

- For later releases, change `$ImageTag` (for example, to `v2`) and repeat the steps, skipping repository creation. Update the AgentCore runtime to use the new image URI.

Run each command only after the previous command succeeds.
