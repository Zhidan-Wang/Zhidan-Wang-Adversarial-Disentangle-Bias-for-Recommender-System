import os
import numpy as np
import random

import torch
import torch.nn as nn

from model import *

import arguments

import utils.load_dataset
import utils.data_loader
import utils.metrics
from utils.early_stop import EarlyStopping, Stop_args

def setup_seed(seed):
    torch.manual_seed(seed)
    if torch.cuda.is_available(): 
        torch.cuda.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

def para(args): 
    if args.dataset == 'yahooR3': 
        args.training_args = {'batch_size': 1024, 'epochs': 500, 'patience': 60, 'block_batch': [6000, 500]}
        args.base_model_args = {'emb_dim': 10, 'learning_rate': 0.001, 'weight_decay': 0}
        args.teacher_model_args = {'emb_dim': 10, 'learning_rate': 0.1, 'weight_decay': 10}
    elif args.dataset == 'coat':
        args.training_args = {'batch_size': 128, 'epochs': 500, 'patience': 60, 'block_batch': [64, 64]}
        args.base_model_args = {'emb_dim': 10, 'learning_rate': 0.001, 'weight_decay': 0.1}
        args.teacher_model_args = {'emb_dim': 10, 'learning_rate': 0.1, 'weight_decay': 10}
    elif args.dataset == 'ml-1m':
        args.training_args = {'batch_size': 1024, 'epochs': 500, 'patience': 60, 'block_batch': [1000, 100]}
        args.base_model_args = {'emb_dim': 10, 'learning_rate': 0.0001, 'weight_decay': 0.1}
        args.teacher_model_args = {'emb_dim': 10, 'learning_rate': 0.1, 'weight_decay': 10}
    else: 
        print('invalid arguments')
        os._exit()
                

def train_and_eval(train_data, unif_data, val_data, test_data, device = 'cuda', 
        model_args: dict = {'emb_dim': 10, 'learning_rate': 0.001, 'weight_decay': 0.0}, teacher_args: dict = {'emb_dim': 10, 'learning_rate': 0.1, 'weight_decay': 10}, 
        training_args: dict =  {'batch_size': 1024, 'epochs': 100, 'patience': 20, 'block_batch': [1000, 100]}): 

    # build data_loader. 
    train_loader = utils.data_loader.Block(train_data, u_batch_size=training_args['block_batch'][0], i_batch_size=training_args['block_batch'][1])
    unif_loader = utils.data_loader.Block(unif_data, u_batch_size=training_args['block_batch'][0], i_batch_size=training_args['block_batch'][1])
    val_loader = utils.data_loader.DataLoader(utils.data_loader.Interactions(val_data), batch_size=training_args['batch_size'], shuffle=False, num_workers=0)
    test_loader = utils.data_loader.DataLoader(utils.data_loader.Interactions(test_data), batch_size=training_args['batch_size'], shuffle=False, num_workers=0)

    # data shape
    n_user, n_item = train_data.shape

    # model and its optimizer. 
    model = MF(n_user, n_item * 2, dim=model_args['emb_dim'], dropout=0).to(device)
    optimizer = torch.optim.SGD(model.parameters(), lr=model_args['learning_rate'], weight_decay=0)
    
    # loss_criterion
    criterion = nn.MSELoss(reduction='sum')

    # begin training
    stopping_args = Stop_args(patience=training_args['patience'], max_epochs=training_args['epochs'])
    early_stopping = EarlyStopping(model, **stopping_args)
    for epo in range(early_stopping.max_epochs):
        training_loss = 0
        for u_batch_idx, users in enumerate(train_loader.User_loader): 
            for i_batch_idx, items in enumerate(train_loader.Item_loader): 
                # loss of training set
                model.train()
                users_train, items_train, y_train = train_loader.get_batch(users, items)
                users_unif, items_unif, y_unif = unif_loader.get_batch(users, items)
                items_unif = items_unif + n_item

                users_combine = torch.cat((users_train, users_unif))
                items_combine = torch.cat((items_train, items_unif))
                y_combine = torch.cat((y_train, y_unif))

                y_hat = model(users_combine, items_combine)

                student_items_embedding = model.item_latent.weight[items_train]
                teacher_items_embedding = torch.detach(model.item_latent.weight[items_train + n_item])
                reg = torch.sum(torch.abs(student_items_embedding - teacher_items_embedding))

                loss = criterion(y_hat, y_combine) + model_args['weight_decay'] * model.l2_norm(users_combine, items_combine) \
                        + teacher_args['weight_decay'] * reg

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                training_loss += loss.item()
            
        model.eval()
        with torch.no_grad():
            # train metrics
            train_pre_ratings = torch.empty(0).to(device)
            train_ratings = torch.empty(0).to(device)
            for u_batch_idx, users in enumerate(train_loader.User_loader): 
                for i_batch_idx, items in enumerate(train_loader.Item_loader): 
                    users_train, items_train, y_train = train_loader.get_batch(users, items)
                    users_unif, items_unif, y_unif = unif_loader.get_batch(users, items)
                    pre_ratings = model(users_train, items_train)
                    train_pre_ratings = torch.cat((train_pre_ratings, pre_ratings))
                    train_ratings = torch.cat((train_ratings, y_train))

            # validation metrics
            val_pre_ratings = torch.empty(0).to(device)
            val_ratings = torch.empty(0).to(device)
            for batch_idx, (users, items, ratings) in enumerate(val_loader):
                pre_ratings = model(users, items)
                val_pre_ratings = torch.cat((val_pre_ratings, pre_ratings))
                val_ratings = torch.cat((val_ratings, ratings))
            
        train_results = utils.metrics.evaluate(train_pre_ratings, train_ratings, ['MSE', 'NLL'])
        val_results = utils.metrics.evaluate(val_pre_ratings, val_ratings, ['MSE', 'NLL', 'AUC'])

        print('Epoch: {0:2d} / {1}, Traning: {2}, Validation: {3}'.
                format(epo, training_args['epochs'], ' '.join([key+':'+'%.3f'%train_results[key] for key in train_results]), 
                ' '.join([key+':'+'%.3f'%val_results[key] for key in val_results])))

        if early_stopping.check([val_results['AUC']], epo):
            break

    # testing loss
    print('Loading {}th epoch'.format(early_stopping.best_epoch))
    model.load_state_dict(early_stopping.best_state)

    # validation metrics
    val_pre_ratings = torch.empty(0).to(device)
    val_ratings = torch.empty(0).to(device)
    for batch_idx, (users, items, ratings) in enumerate(val_loader):
        pre_ratings = model(users, items)
        val_pre_ratings = torch.cat((val_pre_ratings, pre_ratings))
        val_ratings = torch.cat((val_ratings, ratings))

    # test metrics
    test_users = torch.empty(0, dtype=torch.int64).to(device)
    test_items = torch.empty(0, dtype=torch.int64).to(device)
    test_pre_ratings = torch.empty(0).to(device)
    test_ratings = torch.empty(0).to(device)
    for batch_idx, (users, items, ratings) in enumerate(test_loader):
        pre_ratings = model(users, items)
        test_users = torch.cat((test_users, users))
        test_items = torch.cat((test_items, items))
        test_pre_ratings = torch.cat((test_pre_ratings, pre_ratings))
        test_ratings = torch.cat((test_ratings, ratings))

    val_results = utils.metrics.evaluate(val_pre_ratings, val_ratings, ['MSE', 'NLL', 'AUC'])
    test_results = utils.metrics.evaluate(test_pre_ratings, test_ratings, ['MSE', 'NLL', 'AUC', 'Recall_Precision_NDCG@'], users=test_users, items=test_items)
    print('-'*30)
    print('The performance of validation set: {}'.format(' '.join([key+':'+'%.3f'%val_results[key] for key in val_results])))
    print('The performance of testing set: {}'.format(' '.join([key+':'+'%.3f'%test_results[key] for key in test_results])))
    print('-'*30)
    return val_results,test_results

if __name__ == "__main__": 
    args = arguments.parse_args()
    para(args)
    setup_seed(args.seed)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    train, unif_train, validation, test, item_pop = utils.load_dataset.load_dataset(data_name=args.dataset, type = 'explicit', seed = args.seed, device=device)
    train_and_eval(train, unif_train, validation, test, device, model_args = args.base_model_args, teacher_args = args.teacher_model_args, training_args = args.training_args)
