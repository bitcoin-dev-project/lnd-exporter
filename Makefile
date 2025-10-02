PREFIX = bitcoindevproject/lnd-exporter
TAG = 0.2.0

BUILD_DIR = build_output

container:
	docker buildx build --platform linux/amd64,linux/arm64,linux/armhf -t $(PREFIX):$(TAG) .

push: container
	docker push $(PREFIX):$(TAG)

clean:
	-rmdir $(BUILD_DIR)

