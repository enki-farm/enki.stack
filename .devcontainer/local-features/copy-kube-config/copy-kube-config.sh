#!/bin/bash -i

# Copies localhost's ~/.kube/config file into the container and swap out localhost
# for host.docker.internal whenever a new shell starts to keep them in sync.
if [ "$SYNC_LOCALHOST_KUBECONFIG" = "true" ] && [ -d "/usr/local/share/kube-localhost" ]; then
    mkdir -p $HOME/.kube
    sudo cp -r /usr/local/share/kube-localhost/* $HOME/.kube
    sudo chown -R $(id -u) $HOME/.kube
    sed -i -e "s/localhost/host.docker.internal/g" $HOME/.kube/config
    sed -i -e "s/127.0.0.1/host.docker.internal/g" $HOME/.kube/config

    # Keep only the docker-desktop context and its referenced cluster/user.
    #if kubectl --kubeconfig "$HOME/.kube/config" config get-contexts docker-desktop >/dev/null 2>&1; then
    #    kubectl --kubeconfig "$HOME/.kube/config" config use-context docker-desktop >/dev/null 2>&1
    #    if kubectl --kubeconfig "$HOME/.kube/config" config view --raw --minify > "$HOME/.kube/config.min" && [ -s "$HOME/.kube/config.min" ]; then
    #        mv "$HOME/.kube/config.min" "$HOME/.kube/config"
    #    else
    #        rm -f "$HOME/.kube/config.min"
    #    fi
    #fi


    # If .minikube was mounted, set up client cert/key
    if [ -d "/usr/local/share/minikube-localhost" ]; then
        mkdir -p $HOME/.minikube
        sudo cp -r /usr/local/share/minikube-localhost/ca.crt $HOME/.minikube
        # Location varies between versions of minikube
        if [ -f "/usr/local/share/minikube-localhost/client.crt" ]; then
            sudo cp -r /usr/local/share/minikube-localhost/client.crt $HOME/.minikube
            sudo cp -r /usr/local/share/minikube-localhost/client.key $HOME/.minikube
        elif [ -f "/usr/local/share/minikube-localhost/profiles/minikube/client.crt" ]; then
            sudo cp -r /usr/local/share/minikube-localhost/profiles/minikube/client.crt $HOME/.minikube
            sudo cp -r /usr/local/share/minikube-localhost/profiles/minikube/client.key $HOME/.minikube
        fi
        sudo chown -R $(id -u) $HOME/.minikube

        # Point .kube/config to the correct locaiton of the certs
        sed -i -r "s|(\s*certificate-authority:\s).*|\\1$HOME\/.minikube\/ca.crt|g" $HOME/.kube/config
        sed -i -r "s|(\s*client-certificate:\s).*|\\1$HOME\/.minikube\/client.crt|g" $HOME/.kube/config
        sed -i -r "s|(\s*client-key:\s).*|\\1$HOME\/.minikube\/client.key|g" $HOME/.kube/config
    fi
fi